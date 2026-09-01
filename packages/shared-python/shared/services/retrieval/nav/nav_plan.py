"""Structure-conditioned query planning (M2).

``plan_query`` is query-only: it emits a coverage checklist plus an auditable
RetrievalPlan from the user question (no pre-lit map). ``refine_subgoal_query``
may still read a folded planning map after harvest has lit scores.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, List, Literal, Optional, Sequence, Set, Tuple

from .nav_actions import build_legal_actions, format_actionable_map_observation
from .nav_projection import build_map
from .nav_types import NavConfig, NavState, Projection

ContractKind = Literal[
    "single_fact",
    "enumeration",
    "span",
    "comparison",
    "existence",
]
RelationKind = Literal["parent-child", "sibling"]
MapCoverage = Literal["sufficient", "partial", "insufficient"]

_SLOT_REF_RE = re.compile(r"\{\{\s*([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)\s*\}\}")
_CONTRACT_KINDS = {
    "single_fact",
    "enumeration",
    "span",
    "comparison",
    "existence",
}
_RELATION_KINDS = {"parent-child", "sibling"}
_MAP_COVERAGE = {"sufficient", "partial", "insufficient"}
_PLANNER_PURPOSE = "nav_query_plan_v4"
_QUERY_REFINE_PURPOSE = "nav_query_refine_v3"


@dataclass
class Contract:
    kind: ContractKind = "single_fact"
    cardinality: Optional[int] = None


@dataclass
class Subgoal:
    id: str
    need: str
    retrieval_query: str
    depends_on: List[str] = field(default_factory=list)
    prefer_after: List[str] = field(default_factory=list)
    contract: Contract = field(default_factory=Contract)
    produces: List[str] = field(default_factory=list)
    use_node_filter: bool = False
    node_filter_predicates: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class SubgoalEdge:
    source: str
    target: str
    kind: RelationKind


@dataclass
class CoverageItem:
    """One fact that the episode's evidence must eventually cover."""

    id: str
    fact: str


@dataclass
class RetrievalPlan:
    subgoals: List[Subgoal] = field(default_factory=list)
    relations: List[SubgoalEdge] = field(default_factory=list)
    coverage_checklist: List[CoverageItem] = field(default_factory=list)
    reason: str = ""
    map_coverage: MapCoverage = "sufficient"
    fallback: bool = False
    raw: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def subgoal_by_id(self) -> Dict[str, Subgoal]:
        return {s.id: s for s in self.subgoals}


def bind_slots(text: str, bindings: Dict[str, str]) -> str:
    """Fill ``{{slot}}`` / ``{{s1.slot}}`` placeholders from extracted bindings."""

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in bindings:
            return str(bindings[key])
        if "." in key:
            short = key.split(".", 1)[1]
            if short in bindings:
                return str(bindings[short])
        return m.group(0)

    return _SLOT_REF_RE.sub(repl, text or "")


def unbound_slots(text: str) -> List[str]:
    return [m.group(1) for m in _SLOT_REF_RE.finditer(text or "")]


def extract_plan_json(text: str) -> Optional[dict]:
    """Parse a (possibly nested) JSON object from model output.

    Uses brace balancing so nested plan objects are not truncated to the first
    inner ``{...}``.
    """
    s = (text or "").strip()
    if not s:
        return None
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
        s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start : i + 1])
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None


def _as_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_node_filter_predicates(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        raw = raw.get("predicates")
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip().lower()
        if field not in {"path", "summary"}:
            continue
        terms = item.get("terms") or []
        if isinstance(terms, str):
            terms = [terms]
        if not isinstance(terms, list):
            continue
        cleaned = [str(t) for t in terms if str(t)]
        if not cleaned:
            continue
        match = str(item.get("match") or "substring").strip().lower()
        if match not in {"substring", "regex"}:
            match = "substring"
        out.append({"field": field, "terms": cleaned, "match": match})
    return out


def _as_str_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    if isinstance(raw, list):
        out: List[str] = []
        for x in raw:
            t = str(x).strip()
            if t:
                out.append(t)
        return out
    return []


def _parse_contract(raw: Any) -> Contract:
    if not isinstance(raw, dict):
        return Contract()
    kind = str(raw.get("kind") or "single_fact").strip().lower().replace("-", "_")
    if kind not in _CONTRACT_KINDS:
        kind = "single_fact"
    card = raw.get("cardinality", None)
    cardinality: Optional[int] = None
    if card is not None and str(card).strip() != "":
        try:
            cardinality = max(1, int(card))
        except Exception:
            cardinality = None
    return Contract(
        kind=kind,  # type: ignore[arg-type]
        cardinality=cardinality,
    )


def _script_hist(text: str) -> Dict[str, float]:
    """Fraction of non-space chars in cjk / latin / other (no language hardcoding)."""
    counts = {"cjk": 0, "latin": 0, "other": 0}
    for ch in text or "":
        if ch.isspace():
            continue
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            counts["cjk"] += 1
        elif ("A" <= ch <= "Z") or ("a" <= ch <= "z"):
            counts["latin"] += 1
        else:
            counts["other"] += 1
    total = sum(counts.values())
    if total <= 0:
        return {"cjk": 0.0, "latin": 0.0, "other": 0.0}
    return {k: float(v) / float(total) for k, v in counts.items()}


def _dominant_letter_script(hist: Dict[str, float]) -> Optional[str]:
    cjk = float(hist.get("cjk") or 0.0)
    latin = float(hist.get("latin") or 0.0)
    if cjk <= 0.0 and latin <= 0.0:
        return None
    return "cjk" if cjk >= latin else "latin"


def retrieval_query_language_mismatch(retrieval_query: str, reference: str) -> bool:
    """True when rq uses another letter script while lacking the map/query script.

    Criterion is absolute absence of the reference's dominant letter script while
    another letter script is present — not a numeric threshold, not a fixed language.
    """
    rq = (retrieval_query or "").strip()
    ref = (reference or "").strip()
    if not rq or not ref:
        return False
    ref_dom = _dominant_letter_script(_script_hist(ref))
    if ref_dom is None:
        return False
    q_hist = _script_hist(rq)
    if ref_dom == "cjk" and q_hist.get("cjk", 0.0) == 0.0 and q_hist.get("latin", 0.0) > 0.0:
        return True
    if ref_dom == "latin" and q_hist.get("latin", 0.0) == 0.0 and q_hist.get("cjk", 0.0) > 0.0:
        return True
    return False


def language_reference_text(
    *,
    query: str,
    projection: Optional[Projection] = None,
) -> str:
    """Script reference for retrieval_query checks.

    Planner is query-only (projection=None). Refine still sees the folded map;
    its English chrome (collect=/dispatch=/[Hit]) is excluded — only visible
    map *titles* are used so they do not dominate script detection.
    """
    parts: List[str] = []
    q = (query or "").strip()
    if q:
        parts.append(q)
    if projection is not None:
        for v in projection.tree_sections or projection.visible_sections or []:
            title = str(getattr(v, "title", "") or "").strip()
            if not title:
                title = str(getattr(v, "preview", "") or "").strip()
            if title:
                parts.append(title)
    return "\n".join(parts)


def plan_has_language_mismatch(plan: RetrievalPlan, reference: str) -> bool:
    return any(
        retrieval_query_language_mismatch(s.retrieval_query, reference)
        for s in plan.subgoals
    )


def _has_cycle(subgoals: List[Subgoal]) -> bool:
    deps: Dict[str, List[str]] = {s.id: list(s.depends_on) for s in subgoals}
    visiting: Set[str] = set()
    done: Set[str] = set()

    def dfs(node: str) -> bool:
        if node in done:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for nxt in deps.get(node, []):
            if nxt in deps and dfs(nxt):
                return True
        visiting.remove(node)
        done.add(node)
        return False

    return any(dfs(sid) for sid in deps)


def _break_dependency_cycles(subgoals: List[Subgoal]) -> None:
    """Move cycle-forming back-edges to prefer_after; keep acyclic depends_on."""
    deps: Dict[str, List[str]] = {s.id: list(s.depends_on) for s in subgoals}
    visiting: Set[str] = set()
    done: Set[str] = set()
    back_edges: List[Tuple[str, str]] = []

    def dfs(node: str) -> None:
        visiting.add(node)
        for nxt in list(deps.get(node, [])):
            if nxt not in deps:
                continue
            if nxt in visiting:
                back_edges.append((node, nxt))
            elif nxt not in done:
                dfs(nxt)
        visiting.discard(node)
        done.add(node)

    for sid in list(deps.keys()):
        if sid not in done:
            dfs(sid)

    if not back_edges:
        return
    by = {s.id: s for s in subgoals}
    for src, tgt in back_edges:
        s = by.get(src)
        if s is None or tgt not in s.depends_on:
            continue
        s.depends_on = [d for d in s.depends_on if d != tgt]
        if tgt not in s.prefer_after:
            s.prefer_after.append(tgt)


def _slot_owner_and_name(ref: str) -> Tuple[Optional[str], str]:
    token = (ref or "").strip()
    if not token:
        return None, ""
    if "." in token:
        owner, name = token.split(".", 1)
        return owner.strip() or None, name.strip()
    return None, token


def _apply_slot_dependency_inference(subgoals: List[Subgoal]) -> None:
    """Hard deps + produces filled from ``{{s1.slot}}`` references (mechanical)."""
    by_id = {s.id: s for s in subgoals}
    for s in subgoals:
        for ref in unbound_slots(s.retrieval_query) + unbound_slots(s.need):
            owner, name = _slot_owner_and_name(ref)
            if owner and owner in by_id and owner != s.id:
                if owner not in s.depends_on:
                    s.depends_on.append(owner)
                if name and not by_id[owner].produces:
                    by_id[owner].produces = [name]


def _parse_map_coverage(raw: Any) -> MapCoverage:
    val = str(raw or "").strip().lower()
    if val in _MAP_COVERAGE:
        return val  # type: ignore[return-value]
    return "sufficient"


def _parse_coverage_checklist(raw: Any) -> List[CoverageItem]:
    if not isinstance(raw, list):
        return []
    out: List[CoverageItem] = []
    seen: Set[str] = set()
    for i, row in enumerate(raw):
        if isinstance(row, str):
            fact = row.strip()
            cid = f"c{i + 1}"
        elif isinstance(row, dict):
            fact = str(row.get("fact") or row.get("need") or row.get("item") or "").strip()
            cid = str(row.get("id") or f"c{i + 1}").strip() or f"c{i + 1}"
        else:
            continue
        if not fact or cid in seen:
            continue
        seen.add(cid)
        out.append(CoverageItem(id=cid, fact=fact))
    return out


def parse_retrieval_plan(
    obj: dict,
    *,
    query: str,
) -> RetrievalPlan:
    """Parse LLM JSON into a RetrievalPlan; invalid refs are dropped."""
    rows = obj.get("subgoals") or obj.get("goals") or obj.get("steps") or []
    if not isinstance(rows, list):
        rows = []

    subgoals: List[Subgoal] = []
    seen_ids: Set[str] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        sid = str(row.get("id") or row.get("subgoal_id") or f"s{i + 1}").strip()
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)
        need = str(row.get("need") or row.get("goal") or "").strip()
        rq = str(
            row.get("retrieval_query") or row.get("query") or need or query
        ).strip()
        if not need:
            need = rq or query
        if not rq:
            rq = need or query
        produces = _as_str_list(row.get("produces"))
        if len(produces) > 1:
            produces = produces[:1]
        subgoals.append(
            Subgoal(
                id=sid,
                need=need,
                retrieval_query=rq,
                depends_on=_as_str_list(row.get("depends_on")),
                prefer_after=_as_str_list(row.get("prefer_after")),
                contract=_parse_contract(row.get("contract")),
                produces=produces,
                use_node_filter=_as_bool(row.get("use_node_filter")),
                node_filter_predicates=_parse_node_filter_predicates(
                    row.get("node_filter") or row.get("node_filter_predicates")
                ),
            )
        )

    known = {s.id for s in subgoals}
    for s in subgoals:
        s.depends_on = [d for d in s.depends_on if d in known and d != s.id]
        s.prefer_after = [d for d in s.prefer_after if d in known and d != s.id]

    _apply_slot_dependency_inference(subgoals)
    for s in subgoals:
        if len(s.produces) > 1:
            s.produces = s.produces[:1]

    relations: List[SubgoalEdge] = []
    rel_raw = obj.get("relations") or obj.get("edges") or []
    if isinstance(rel_raw, list):
        for edge in rel_raw:
            if not isinstance(edge, dict):
                continue
            src = str(edge.get("source") or edge.get("from") or edge.get("a") or "").strip()
            tgt = str(edge.get("target") or edge.get("to") or edge.get("b") or "").strip()
            kind = str(edge.get("kind") or edge.get("relation") or "").strip().lower()
            if kind == "parent_child":
                kind = "parent-child"
            if kind == "unrelated":
                continue
            if src not in known or tgt not in known or src == tgt:
                continue
            if kind not in _RELATION_KINDS:
                continue
            relations.append(SubgoalEdge(source=src, target=tgt, kind=kind))  # type: ignore[arg-type]
            if kind == "parent-child":
                for s in subgoals:
                    if s.id == tgt and src not in s.depends_on:
                        s.depends_on.append(src)

    if _has_cycle(subgoals):
        _break_dependency_cycles(subgoals)

    reason = str(obj.get("reason") or obj.get("plan_reason") or "").strip()
    coverage = _parse_map_coverage(obj.get("map_coverage") or obj.get("coverage"))
    checklist = _parse_coverage_checklist(
        obj.get("coverage_checklist") or obj.get("checklist")
    )
    if not checklist:
        checklist = [
            CoverageItem(id=f"c{i + 1}", fact=s.need)
            for i, s in enumerate(subgoals)
            if (s.need or "").strip()
        ]
    return RetrievalPlan(
        subgoals=subgoals,
        relations=relations,
        coverage_checklist=checklist,
        reason=reason,
        map_coverage=coverage,
    )


def fallback_plan(query: str, *, reason: str = "fallback") -> RetrievalPlan:
    """Single shared-space subgoal when planning fails or is unnecessary."""
    q = (query or "").strip() or "query"
    return RetrievalPlan(
        subgoals=[
            Subgoal(
                id="s1",
                need=q,
                retrieval_query=q,
                contract=Contract(kind="single_fact"),
            )
        ],
        coverage_checklist=[CoverageItem(id="c1", fact=q)],
        reason=reason,
        map_coverage="sufficient",
        fallback=True,
    )


def validate_retrieval_plan(plan: RetrievalPlan) -> Tuple[bool, str]:
    if not plan.subgoals:
        return False, "empty_subgoals"
    ids = [s.id for s in plan.subgoals]
    if len(ids) != len(set(ids)):
        return False, "duplicate_ids"
    known = set(ids)
    for s in plan.subgoals:
        if not (s.need or s.retrieval_query):
            return False, f"empty_need:{s.id}"
        for d in s.depends_on:
            if d not in known:
                return False, f"bad_depends_on:{s.id}->{d}"
        for d in s.prefer_after:
            if d not in known:
                return False, f"bad_prefer_after:{s.id}->{d}"
        if s.contract.kind not in _CONTRACT_KINDS:
            return False, f"bad_contract:{s.id}"
        if len(s.produces) > 1:
            return False, f"multi_produces:{s.id}"
        for ref in unbound_slots(s.retrieval_query) + unbound_slots(s.need):
            owner, _name = _slot_owner_and_name(ref)
            if owner and owner in known and owner not in s.depends_on and owner != s.id:
                return False, f"slot_missing_depends:{s.id}->{owner}"
    if plan.map_coverage not in _MAP_COVERAGE:
        return False, "bad_map_coverage"
    if _has_cycle(plan.subgoals):
        return False, "cyclic_depends_on"
    return True, "ok"


def planning_char_limit(config: NavConfig) -> int:
    planned = int(getattr(config, "planning_map_char_limit", 0) or 0)
    base = int(config.map_char_limit or 0)
    if planned > 0:
        return planned
    return max(1, base)


def build_planning_observation(
    ts: Any,
    state: NavState,
    config: NavConfig,
) -> Tuple[Projection, str]:
    """Build the one-shot planning map (wider budget; same fold/title-only rules)."""
    limit = planning_char_limit(config)
    plan_cfg = replace(config, map_char_limit=limit)
    projection = build_map(
        ts,
        doc_id=state.doc_id,
        query=state.query,
        scope=None,
        config=plan_cfg,
        map_scores=state.map_scores,
        collected_section_ids=state.collected_section_ids,
        dismissed_section_ids=state.dismissed_section_ids,
        highlight_ids=state.highlight_ids,
        harvested_section_ids=(
            state.harvested_owner_subgoal if config.is_checklist else None
        ),
    )
    actions = build_legal_actions(
        state,
        projection,
        step_idx=0,
        config=plan_cfg,
        depth=0,
        ts=ts,
    )
    obs = format_actionable_map_observation(
        projection,
        actions,
        inline_summary=False,
    )
    return projection, obs


def _planner_system_prompt(*, max_subgoals: int) -> str:
    cap = ""
    if max_subgoals > 0:
        cap = f" Prefer at most {max_subgoals} subgoals."
    return (
        "You are a retrieval planner. You receive only the user query (no "
        "document map). Emit a coverage checklist plus a retrieval plan over "
        "ONE shared search space — do not invent per-subgoal corpus partitions.\n\n"
        "Rules:\n"
        "1. coverage_checklist lists the facts that episode evidence must cover "
        "(short, concrete facts in the query's language)."
        f"{cap}\n"
        "2. Default to a SINGLE subgoal. Only add more subgoals for a hard data "
        "dependency ({{s1.slot}} in a later query) or for clearly independent "
        "cross-entity comparisons. Do not split merely to list checklist items.\n"
        "3. Each subgoal produces at most ONE slot name in produces "
        "(enumeration = one list-valued slot).\n"
        "4. retrieval_query is a SHORT KEYWORD QUERY for THIS subgoal only "
        "(space-separated entity/role/topic tokens, e.g. \"王仁坤 总工程师 设计成果\"). "
        "Split or adapt the user question into compact lexical terms — do NOT "
        "emit a full natural-language question or long prose. Downstream lexical "
        "retrieval scores and lights the map from these tokens, so keep entity "
        "names and role terms; drop filler words. Same language/script as the "
        "user query — do not translate terms into another script.\n"
        "5. If a later retrieval_query needs a value from an earlier subgoal, "
        "write it as {{s1.slot}} (not prose). That implies depends_on.\n"
        "6. depends_on = hard data dependency. prefer_after = soft ordering only.\n"
        "7. All subgoals share one search space — do not invent per-subgoal scopes.\n"
        "8. relations only for parent-child or sibling (omit unrelated pairs).\n"
        "9. map_coverage: sufficient | partial | insufficient — whether the user "
        "query alone is enough to write executable retrieval_query terms "
        "(use sufficient unless the query is empty or unusable).\n"
        "10. reason must be English, under 40 words.\n"
        "11. Set use_node_filter=true when the subgoal enumerates or compares "
        "named facets you can write as path predicates (filenames, section "
        "titles). Keep it false for vague semantic needs; "
        "retrieval_query remains the fuzzy-leg fallback. Optional node_filter "
        "may seed predicates: "
        '[{\"field\":\"path\",\"terms\":[\"...\"],\"match\":\"substring|regex\"}].\n\n'
        "Return ONLY one JSON object:\n"
        "{\n"
        '  "reason": "...",\n'
        '  "map_coverage": "sufficient|partial|insufficient",\n'
        '  "coverage_checklist": [\n'
        '    {"id": "c1", "fact": "..."},\n'
        '    {"id": "c2", "fact": "..."}\n'
        "  ],\n"
        '  "subgoals": [\n'
        "    {\n"
        '      "id": "s1",\n'
        '      "need": "...",\n'
        '      "retrieval_query": "...",\n'
        '      "depends_on": [],\n'
        '      "prefer_after": [],\n'
        '      "produces": ["entity"],\n'
        '      "contract": {"kind": "single_fact|enumeration|span|comparison|existence", '
        '"cardinality": null},\n'
        '      "use_node_filter": false,\n'
        '      "node_filter": []\n'
        "    },\n"
        "    {\n"
        '      "id": "s2",\n'
        '      "need": "...",\n'
        '      "retrieval_query": "... {{s1.entity}} ...",\n'
        '      "depends_on": ["s1"],\n'
        '      "produces": ["detail"],\n'
        '      "contract": {"kind": "single_fact"}\n'
        "    }\n"
        "  ],\n"
        '  "relations": [{"source": "s1", "target": "s2", "kind": "parent-child|sibling"}]\n'
        "}"
    )


def _language_repair_user(
    state: NavState,
    bad_plan: RetrievalPlan,
    *,
    reference: str,
) -> str:
    bad_lines = []
    for s in bad_plan.subgoals:
        if retrieval_query_language_mismatch(s.retrieval_query, reference):
            bad_lines.append(f"- {s.id}: {s.retrieval_query}")
    bad_block = "\n".join(bad_lines) if bad_lines else "(see prior plan)"
    return (
        f"User query: {state.query}\n"
        f"Task type: {state.task_type}\n\n"
        "Your previous plan had retrieval_query values that do not match the "
        "language/script of the user query. Those queries will fail lexical "
        "retrieval. Rewrite the FULL plan JSON. Keep structure, but rewrite "
        "every mismatched retrieval_query as a short keyword query in the "
        "query's own language and terms (space-separated tokens, not a "
        "full-sentence question).\n"
        f"Mismatched retrieval_query lines:\n{bad_block}\n"
    )


def plan_query(
    state: NavState,
    config: NavConfig,
) -> RetrievalPlan:
    """LLM plan from the user query only (no pre-lit map). Falls back to one subgoal."""
    from .nav_llm import (  # type: ignore
        nav_chat,
        planner_output_max_tokens,
        resolve_nav_model,
        resolve_nav_thinking_mode,
    )
    from .nav_token_budget import NavTokenLimit, nav_token_budget_exhausted

    if nav_token_budget_exhausted():
        return fallback_plan(state.query, reason="token_limit")

    model = resolve_nav_model(
        model=config.planner_model,
        model_env="NAV_PLANNER_MODEL",
        fallback_envs=("NAV_LLM_MODEL",),
    )
    thinking_mode = resolve_nav_thinking_mode(role="planner")
    # Thinking can exceed the default 60s client timeout.
    timeout_s = float(os.environ.get("NAV_PLANNER_TIMEOUT_SECONDS", "").strip() or "0")
    if timeout_s <= 0:
        timeout_s = 300.0 if thinking_mode == "enabled" else 90.0
    max_subgoals = int(getattr(config, "planner_max_subgoals", 0) or 0)
    system = _planner_system_prompt(max_subgoals=max_subgoals)
    reference = language_reference_text(query=state.query)
    user = (
        f"User query: {state.query}\n"
        f"Task type: {state.task_type}\n\n"
        "Return the retrieval plan JSON."
    )
    max_tokens = planner_output_max_tokens(
        max(
            int(config.llm_max_tokens or 0),
            int(getattr(config, "planner_llm_max_tokens", 0) or 0),
            256,
        )
    )
    last_raw = ""
    last_err = ""
    language_repair_used = False
    for _attempt in range(3):
        try:
            cached = nav_chat(
                purpose=_PLANNER_PURPOSE,
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=float(config.llm_temperature),
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                thinking_role="planner",
                context="Nav Query Planner",
                api_key_env="NAV_PLANNER_API_KEY",
                base_url_env="NAV_PLANNER_BASE_URL",
                timeout=timeout_s,
                usage_tag="nav_plan",
            )
            content = str(cached.get("content") or "").strip()
            reasoning = str(cached.get("reasoning_content") or "").strip()
            last_raw = content
            if not last_raw and reasoning:
                # Only fall back to reasoning if it actually embeds JSON.
                if extract_plan_json(reasoning):
                    last_raw = reasoning
                else:
                    last_err = "empty_content_after_think"
                    continue
            if not last_raw:
                last_err = "empty_content"
                continue
            obj = extract_plan_json(last_raw) or {}
            plan = parse_retrieval_plan(obj, query=state.query)
            plan.raw = last_raw[:2000]
            ok, why = validate_retrieval_plan(plan)
            if not ok:
                last_err = why
                continue
            if plan_has_language_mismatch(plan, reference) and not language_repair_used:
                language_repair_used = True
                last_err = "language_mismatch"
                user = _language_repair_user(state, plan, reference=reference)
                continue
            if plan_has_language_mismatch(plan, reference):
                last_err = "language_mismatch"
                continue
            return plan
        except NavTokenLimit:
            return fallback_plan(state.query, reason="token_limit")
        except Exception as exc:  # pragma: no cover - network/LLM path
            last_err = str(exc)
            continue
    plan = fallback_plan(state.query, reason=f"fallback:{last_err or 'unparsed'}")
    plan.raw = last_raw[:2000]
    return plan


def _refine_system_prompt() -> str:
    return (
        "You rewrite ONE retrieval_query for a single subgoal on a hierarchical "
        "document map.\n"
        "A previous attempt for THIS subgoal failed. A controller reported what "
        "is still missing. You see the folded title map.\n\n"
        "Rules:\n"
        "1. Scope is this subgoal only. Rewrite for its need; do not pull in "
        "other subgoals' goals or other parts of a multi-part question.\n"
        "2. Write a SHORT KEYWORD QUERY (space-separated tokens), not a full "
        "sentence. Start from the previous retrieval_query and revise by "
        "adding, dropping, or swapping terms so it aims at the reported gap. "
        "A paraphrase that ranks the same nodes is useless.\n"
        "3. If prior selected nodes are listed, do not aim the question back at "
        "those same nodes; steer toward other map regions that may hold the "
        "missing evidence, using the map's own wording where helpful.\n"
        "4. Same language/script as the map titles. Keep entity names and scope "
        "constraints. Do not paste the controller's note verbatim.\n"
        "5. Keep it under 40 words, no commentary.\n\n"
        'Return ONLY one JSON object: {"retrieval_query": "..."}'
    )


def _section_titles_for_refine(ts: Any, section_ids: Sequence[str]) -> List[str]:
    """Titles of previously selected nodes, falling back to the section id."""
    out: List[str] = []
    seen: Set[str] = set()
    for raw in section_ids or ():
        sid = str(raw or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        title = ""
        try:
            st = ts.get_structure(sid) or {}
            title = str(st.get("title") or "").strip()
        except Exception:
            title = ""
        out.append(title or sid)
    return out


def refine_subgoal_query(
    ts: Any,
    state: NavState,
    config: NavConfig,
    *,
    subgoal: Subgoal,
    previous_query: str,
    gap: str,
    selected_section_ids: Optional[Sequence[str]] = None,
) -> str:
    """PLAN-side rewrite of one subgoal's retrieval_query after ``widen``.

    ``plan_control`` can say *what* is missing but never sees the map, so it
    cannot write a query that matches the corpus' own terms. This is the one
    call that can: it reads the planning map and turns the gap into new search
    terms for THIS subgoal only. Returns "" when the rewrite fails, echoes the
    previous query, or lands in the wrong script — the caller then keeps the
    previous query.
    """
    from .nav_llm import nav_chat, resolve_nav_model
    from .nav_token_budget import NavTokenLimit, nav_token_budget_exhausted

    prev = (previous_query or subgoal.retrieval_query or "").strip()
    need = (subgoal.need or prev).strip()
    if nav_token_budget_exhausted():
        return ""

    projection, observation = build_planning_observation(ts, state, config)
    # Language check against this subgoal + map titles only (no episode query).
    reference = language_reference_text(query=need, projection=projection)
    selected_titles = _section_titles_for_refine(ts, selected_section_ids or ())
    if selected_titles:
        selected_block = "Prior selected nodes:\n" + "\n".join(
            f"- {t}" for t in selected_titles
        )
    else:
        selected_block = "Prior selected nodes: (none)"
    model = resolve_nav_model(
        model=config.planner_model,
        model_env="NAV_PLANNER_MODEL",
        fallback_envs=("NAV_LLM_MODEL",),
    )
    user = (
        f"Subgoal need: {need}\n"
        f"Previous retrieval_query (failed): {prev}\n"
        f"Reported gap: {(gap or '').strip() or '(none given)'}\n"
        f"{selected_block}\n\n"
        f"=== Planning Map ===\n{observation}\n=== End Planning Map ===\n\n"
        "Return the rewritten retrieval_query JSON."
    )
    try:
        cached = nav_chat(
            purpose=_QUERY_REFINE_PURPOSE,
            model=model,
            messages=[
                {"role": "system", "content": _refine_system_prompt()},
                {"role": "user", "content": user},
            ],
            temperature=float(config.llm_temperature),
            max_tokens=max(128, int(config.llm_max_tokens or 0)),
            response_format={"type": "json_object"},
            thinking_role="action",
            context="Nav Query Refine",
            api_key_env="NAV_PLANNER_API_KEY",
            base_url_env="NAV_PLANNER_BASE_URL",
            usage_tag="nav_query_refine",
        )
    except NavTokenLimit:
        return ""
    except Exception:  # pragma: no cover - network/LLM path
        return ""

    obj = extract_plan_json(str(cached.get("content") or "")) or {}
    refined = str(obj.get("retrieval_query") or obj.get("query") or "").strip()
    if not refined or refined == prev:
        return ""
    if retrieval_query_language_mismatch(refined, reference):
        return ""
    return refined
