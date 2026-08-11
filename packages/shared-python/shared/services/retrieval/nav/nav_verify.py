"""Slot binding helpers for plan orchestration.

Extracts values only when a later subgoal's ``{{sN.slot}}`` (or short
``{{slot}}`` matching this subgoal's ``produces``) needs them.
Checklist reconciliation belongs solely to ``nav_control.plan_control``.
Slot fill is LLM-only (no lexical/heuristic line dump).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .nav_plan import RetrievalPlan, Subgoal, unbound_slots
from .nav_types import NavConfig, SubgoalResult


def evidence_chars(text: str) -> int:
    return len((text or "").strip())


def _slot_owner_and_name(ref: str) -> Tuple[Optional[str], str]:
    token = (ref or "").strip()
    if not token:
        return None, ""
    if "." in token:
        owner, name = token.split(".", 1)
        return owner.strip() or None, name.strip()
    return None, token


def demanded_slot_names(plan: RetrievalPlan, producer: Subgoal) -> List[str]:
    """Slots this subgoal must fill because a *later* subgoal references them."""
    wanted: Set[str] = set()
    produces = {str(p).strip() for p in (producer.produces or []) if str(p).strip()}
    for sg in plan.subgoals:
        if sg.id == producer.id:
            continue
        for ref in unbound_slots(sg.retrieval_query) + unbound_slots(sg.need):
            owner, name = _slot_owner_and_name(ref)
            if not name:
                continue
            if owner == producer.id:
                wanted.add(name)
            elif owner is None and name in produces:
                wanted.add(name)
    if produces:
        # Preserve planner order; drop produces nobody consumes.
        return [p for p in (producer.produces or []) if str(p).strip() in wanted]
    return sorted(wanted)


def extract_slots_llm(
    slots: Sequence[str],
    evidence_text: str,
    config: NavConfig,
    *,
    need: str = "",
    retrieval_query: str = "",
    contract_kind: str = "single_fact",
    cardinality: Optional[int] = None,
    steps_out: Optional[List[Any]] = None,
    subgoal_id: str = "",
) -> Dict[str, str]:
    """Ask the LLM for slot values; empty on failure."""
    names = [str(s).strip() for s in slots if str(s).strip()]
    if not names or not (evidence_text or "").strip():
        return {}
    try:
        from .nav_llm import nav_chat, resolve_nav_model
        from .nav_token_budget import nav_token_budget_exhausted, stamp_step_detail
    except Exception:
        return {}
    if nav_token_budget_exhausted():
        return {}

    model = resolve_nav_model(
        model=config.planner_model,
        model_env="NAV_PLANNER_MODEL",
        fallback_envs=("NAV_LLM_MODEL",),
    )
    system = (
        "Extract slot values for a retrieval subgoal from evidence text.\n"
        'Return ONLY JSON: {"slots": {"name": "value"}, "confidence": 0..1}.\n'
        "Use the evidence language. If a slot is missing, omit it.\n"
        "Do not invent facts absent from the evidence."
    )
    user = (
        f"Need: {need}\n"
        f"Retrieval query: {retrieval_query or need}\n"
        f"Slots to fill: {json.dumps(names, ensure_ascii=False)}\n"
        f"Contract: {contract_kind}"
        + (f", cardinality={cardinality}" if cardinality is not None else "")
        + f"\n\n=== Evidence ===\n{evidence_text[:6000]}\n=== End Evidence ===\n"
    )
    import time

    t0 = time.perf_counter()
    out: Dict[str, str] = {}
    try:
        cached = nav_chat(
            purpose="nav_slot_extract_v1",
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=float(config.llm_temperature),
            max_tokens=max(256, int(config.llm_max_tokens or 256)),
            response_format={"type": "json_object"},
            context="Nav Slot Extract",
            api_key_env="NAV_PLANNER_API_KEY",
            base_url_env="NAV_PLANNER_BASE_URL",
            usage_tag="nav_slot_extract",
        )
        raw = str(cached.get("content") or "").strip()
        obj = json.loads(raw) if raw.startswith("{") else {}
        slots_obj = obj.get("slots") if isinstance(obj, dict) else None
        if isinstance(slots_obj, dict):
            for name in names:
                val = str(slots_obj.get(name) or "").strip()
                if val:
                    out[name] = val
    except Exception:
        out = {}
    if steps_out is not None:
        try:
            from ._compat import AgentStep  # type: ignore
        except Exception:
            AgentStep = None  # type: ignore
        if AgentStep is not None:
            steps_out.append(
                AgentStep(
                    step_idx=len(steps_out) + 1,
                    action="slot_extract",
                    detail=stamp_step_detail(
                        {
                            "subgoal_id": subgoal_id,
                            "slots_requested": names,
                            "slots_filled": sorted(out.keys()),
                        },
                        t0=t0,
                    ),
                )
            )
    return out


def extract_slots(
    plan: RetrievalPlan,
    subgoal: Subgoal,
    evidence_text: str,
    config: NavConfig,
    *,
    retrieval_query: str = "",
    steps_out: Optional[List[Any]] = None,
) -> Tuple[Dict[str, str], float]:
    """LLM-fill slots demanded by downstream subgoals; empty if none demanded."""
    slots = demanded_slot_names(plan, subgoal)
    if not slots:
        conf = 1.0 if (evidence_text or "").strip() else 0.0
        return {}, conf
    filled = extract_slots_llm(
        slots,
        evidence_text,
        config,
        need=subgoal.need,
        retrieval_query=retrieval_query or subgoal.retrieval_query,
        contract_kind=subgoal.contract.kind,
        cardinality=subgoal.contract.cardinality,
        steps_out=steps_out,
        subgoal_id=str(subgoal.id or ""),
    )
    n = sum(1 for s in slots if (filled.get(s) or "").strip())
    return filled, float(n) / float(len(slots))


def build_subgoal_result(
    plan: RetrievalPlan,
    state_collected_section_ids: Sequence[str],
    config: NavConfig,
    subgoal: Subgoal,
    *,
    retrieval_query: str,
    new_chunks: Sequence[Tuple[Any, float]],
    collected_before: Set[str],
    explicit_collect_ids: Optional[Sequence[str]] = None,
    explicit_before: Optional[Set[str]] = None,
    steps_out: Optional[List[Any]] = None,
) -> SubgoalResult:
    """Package this wave's evidence + optional downstream slot bindings.

    ``satisfied`` here means "this wave collected non-empty evidence" — bookkeeping
    for dependency readiness when plan_control is off. Checklist acceptance is
    decided only by ``plan_control``.
    """
    evidence = build_evidence_text_from_chunks(new_chunks)
    chars = evidence_chars(evidence)
    extracted, conf = extract_slots(
        plan,
        subgoal,
        evidence,
        config,
        retrieval_query=retrieval_query,
        steps_out=steps_out,
    )
    before_explicit = set(explicit_before or ())
    explicit_wave = [
        str(s).strip()
        for s in (explicit_collect_ids or ())
        if str(s).strip() and str(s).strip() not in before_explicit
    ]
    seen: Set[str] = set()
    explicit_ordered: List[str] = []
    for sid in explicit_wave:
        if sid in seen:
            continue
        seen.add(sid)
        explicit_ordered.append(sid)
    return SubgoalResult(
        subgoal_id=subgoal.id,
        satisfied=chars > 0,
        confidence=float(conf),
        extracted=dict(extracted),
        chars_used=chars,
        gap="empty_evidence" if chars <= 0 else "",
        collected_section_ids=[
            s for s in state_collected_section_ids if s not in collected_before
        ],
        explicit_collect_ids=explicit_ordered,
    )


def apply_bindings_from_result(
    bindings: Dict[str, str],
    subgoal: Subgoal,
    extracted: Dict[str, str],
) -> Dict[str, str]:
    """Write short and qualified ``s1.slot`` keys into bindings."""
    out = dict(bindings or {})
    for slot, value in (extracted or {}).items():
        name = str(slot).strip()
        val = str(value or "").strip()
        if not name or not val:
            continue
        out[name] = val
        out[f"{subgoal.id}.{name}"] = val
    return out


def build_evidence_text_from_chunks(chunks: Any, *, limit: int = 8000) -> str:
    """Concatenate (chunk, score) texts up to a char budget."""
    parts: List[str] = []
    total = 0
    for chunk, _score in list(chunks or []):
        text = str(getattr(chunk, "text", "") or getattr(chunk, "content", "") or "")
        if not text.strip():
            continue
        if total >= limit:
            break
        take = text[: max(0, limit - total)]
        parts.append(take)
        total += len(take)
    return "\n".join(parts)
