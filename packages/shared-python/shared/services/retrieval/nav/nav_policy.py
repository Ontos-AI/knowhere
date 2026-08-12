from __future__ import annotations

import json
import re
import time
from typing import Any, List, Optional

from .nav_actions import action_by_id, actions_by_ids
from .nav_types import ActionKind, LegalAction, NavConfig, NavState, Projection


def choose_rule_action(
    state: NavState,
    projection: Projection,
    actions: List[LegalAction],
    *,
    step_idx: int,
    config: NavConfig,
) -> LegalAction:
    """Deterministic pick for ``policy=rule`` (and rare last-resort paths).

    Illegal LLM action_ids do **not** use this COLLECT-first path; they FINISH
    the scope and record ``refusal_events`` (see ``choose_llm_action``).
    """
    del state, projection, step_idx, config

    def first(kind: ActionKind) -> Optional[LegalAction]:
        for a in actions:
            if a.kind == kind:
                return a
        return None

    act = first(ActionKind.COLLECT) or first(ActionKind.DISPATCH)
    if act:
        return act
    return first(ActionKind.FINISH) or actions[-1]


def _finish_or_rule(
    state: NavState,
    projection: Projection,
    actions: List[LegalAction],
    *,
    step_idx: int,
    config: NavConfig,
) -> LegalAction:
    finish = next((a for a in actions if a.kind == ActionKind.FINISH), None)
    if finish is not None:
        return finish
    return choose_rule_action(
        state, projection, actions, step_idx=step_idx, config=config
    )


def _extract_json_obj(text: str) -> Optional[dict]:
    s = (text or "").strip().replace("```json", "").replace("```", "")
    if not s:
        return None
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass
    m = re.search(r"\{.*?\}", s, flags=re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _extract_action_id_fallback(text: str) -> str:
    s = (text or "").strip()
    for key in ("action_id", "id"):
        m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', s, flags=re.I)
        if m:
            return str(m.group(1) or "").strip().upper()
    return ""


def _normalize_id_list(obj: dict, primary_aid: str) -> List[str]:
    """Union of action_id and optional ids into one ordered selected set."""
    ids_raw = obj.get("ids") or obj.get("action_ids") or obj.get("action_args", {})
    ids: List[str] = []
    if isinstance(ids_raw, dict):
        ids_raw = ids_raw.get("ids") or []
    if isinstance(ids_raw, list):
        ids = [str(x).strip().upper() for x in ids_raw if str(x).strip()]
    if primary_aid and primary_aid not in ids:
        ids = [primary_aid] + ids
    out: List[str] = []
    seen: set[str] = set()
    for i in ids:
        if i not in seen:
            out.append(i)
            seen.add(i)
    return out


def _collect_roots_from_history(state: NavState) -> List[str]:
    """Sections the agent explicitly COLLECT'd (not descendant auto-marks)."""
    roots: List[str] = []
    seen: set[str] = set()
    for h in state.action_history:
        if h.get("kind") != "collect":
            continue
        if int(h.get("n_added", 0) or 0) <= 0:
            continue
        sid = str(h.get("section_id") or "").strip()
        if sid and sid not in seen:
            roots.append(sid)
            seen.add(sid)
    return roots


def _format_agent_state(
    state: NavState,
    step_idx: int,
    config: NavConfig,
    *,
    max_steps: Optional[int] = None,
) -> str:
    """Agent state block shown before the actionable observation."""
    episode_steps = int(max_steps if max_steps is not None else config.max_steps)
    lines = ["=== Agent State ==="]
    lines.append(f"Current scope: {state.current_scope or 'document-root'}")
    lines.append(f"Step: {step_idx + 1} / {episode_steps}")
    lines.append(
        "Observation mode: folded hierarchy map "
        "(title-only at document-root; summaries inline inside dispatched regions)"
    )

    roots = _collect_roots_from_history(state)
    if roots:
        lines.append(f"Evidence collected: {len(roots)} section(s)")
        for sid in roots:
            lines.append(f'  - "{sid}"')
    else:
        lines.append("Evidence collected: none")

    investigated = sorted(state.investigated_section_ids)
    if investigated:
        lines.append(f"Regions investigated (subagent reports below): {len(investigated)}")
        for sid in investigated[:20]:
            lines.append(f'  - "{sid}"')
        if len(investigated) > 20:
            lines.append(f"  - ... (+{len(investigated) - 20} more)")

    remaining = episode_steps - step_idx - 1
    if remaining <= 2:
        lines.append(
            f"Only {remaining} step(s) remaining. Prefer COLLECT or FINISH if evidence is sufficient."
        )

    lines.append("=== End Agent State ===")
    return "\n".join(lines)


def _system_prompt(
    *,
    depth: int = 0,
    dispatch_available: bool = True,
    has_preview: bool = False,
) -> str:
    """Observe-act navigate prompt (COLLECT / DISPATCH / FINISH).

    The prompt is state-adaptive: when no DISPATCH action is legal at this layer
    (e.g. recursion off and depth>0), all DISPATCH semantics/examples/preferences
    are removed and the model is told this layer is COLLECT/FINISH only. This
    prevents the model from emitting illegal D* that would finish the scope.

    When has_preview is True (depth-0 with assembled evidence groups), FINISH must
    include a relative group_rank over [G*] ids.

    Style follows KNOWHERE collector rules (action IDs on node lines, English
    reason, no invented targets). Asset SEARCH is via harvest search_assets.
    """
    role = (
        "You are a document navigation agent running an observe-act loop."
        if depth == 0
        else "You are a region subagent investigating one assigned document subtree."
    )

    ids_hint = (
        '"ids": ["C1","C3",...] or ["D1","D2",...]'
        if dispatch_available
        else '"ids": ["C1","C3",...]'
    )

    action_semantics = [
        "Action semantics:",
        "  - collect=C*: add each selected section to evidence. A parent section "
        "hydrates its full subtree; a leaf adds only that section. "
        "For every selected collect id, provide confidence in [0,1] "
        "(object map keyed by action id, or a single scalar for one id).",
    ]
    if dispatch_available:
        action_semantics.append(
            "  - dispatch=D*: hand the listed region(s) to a child subagent "
            "explorers; you receive their reports without moving your own viewpoint."
        )
    action_semantics.append("  - finish=F*: end navigation for this scope / document.")

    scope_rule = (
        "  - This layer is COLLECT/FINISH only: there is NO dispatch action here. "
        "Do not output any D* id; COLLECT the relevant sections directly.\n"
        if not dispatch_available
        else ""
    )
    prefer_rule = (
        "  - Prefer DISPATCH for large internal sections when available; prefer "
        "COLLECT for leaves or small clearly-relevant sections.\n"
        if dispatch_available
        else "  - COLLECT the sections relevant to the query; use FINISH when done.\n"
    )

    preview_rule = ""
    if has_preview:
        preview_rule = (
            "  - When Assembled Evidence ([G*] groups) is shown, FINISH MUST include "
            '"group_rank": an ordered list of those G* ids, most relevant to the '
            "query first (relative ranking, not absolute scores).\n"
            "  - For list/coverage queries, FINISH only if the assembled groups already "
            "cover ALL required items; otherwise COLLECT the missing ones first.\n"
        )

    examples = [
        "Return ONLY one JSON object, e.g.:",
        '{"action_id":"C1","confidence":0.8,"reason":"short reason"}',
        'Batch: {"action_id":"C1","ids":["C1","C3"],'
        '"confidence":{"C1":0.7,"C3":0.9},"reason":"..."}',
    ]
    if dispatch_available:
        examples.append(
            'Batch: {"action_id":"D1","ids":["D1","D2"],"reason":"..."}'
        )
    if has_preview:
        examples.append(
            'Finish with rank: {"action_id":"F1","group_rank":["G2","G1"],"reason":"..."}'
        )

    return (
        f"{role}\n\n"
        "The observation is a folded hierarchy map. Each visible node lists only the "
        "action IDs currently legal for that node. Collected branches are removed from "
        "the map. At document-root the map is title-only; inside a dispatched region, "
        "node summaries are inlined.\n\n"
        "=== Rules ===\n\n"
        f"Select one or more action IDs of the same kind. Put them in action_id and "
        f"optional {ids_hint}; the final selection is their union. "
        "Hydration is decided by hierarchy after selection "
        "(parent COLLECT = full subtree; leaf COLLECT = that section only).\n\n"
        + "\n".join(action_semantics)
        + "\n\n"
        "  - Use only action IDs shown on a node line or under Global actions. "
        "Never invent IDs or write raw section paths as targets.\n"
        + scope_rule
        + prefer_rule
        + preview_rule
        +         "  - Do NOT re-collect a section already listed under Evidence collected.\n"
        "  - FINISH when this scope is done: evidence is sufficient, or this region is "
        "irrelevant / exhausted (especially as a subagent). "
        "The system will not infer missing evidence for you.\n"
        "  - When steps remaining <= 2, prioritize COLLECT or FINISH.\n\n"
        "=== End Rules ===\n\n"
        + "\n".join(examples)
        + "\n"
        "Do not include any explanation outside the JSON.\n\n"
        "IMPORTANT:\n"
        "1. All agent-generated text (reason) MUST be in English.\n"
        "2. Document content and section titles MUST remain in their original language.\n"
        "3. Keep reason under 25 words.\n"
        "4. COLLECT must include confidence for each selected collect id.\n"
    )


def choose_llm_action(
    state: NavState,
    projection: Projection,
    actions: List[LegalAction],
    *,
    step_idx: int,
    config: NavConfig,
    depth: int = 0,
    max_steps: Optional[int] = None,
    group_map: Optional[dict[str, str]] = None,
    assembled_preview: Optional[str] = None,
) -> tuple[LegalAction, dict]:
    from .nav_llm import nav_chat, resolve_nav_model
    from .nav_token_budget import NavTokenLimit, nav_token_budget_exhausted

    def _token_limit_finish() -> tuple[LegalAction, dict]:
        finish = _finish_or_rule(
            state, projection, actions, step_idx=step_idx, config=config
        )
        return finish, {
            "reason": "token_limit",
            "stop_reason": "token_limit",
            "depth": depth,
        }

    if nav_token_budget_exhausted():
        return _token_limit_finish()

    model = resolve_nav_model(
        model=(config.subagent_model if depth > 0 else config.llm_model),
        model_env=("NAV_SUBAGENT_MODEL" if depth > 0 else "NAV_LLM_MODEL"),
        fallback_envs=("NAV_LLM_MODEL",),
    )
    agent_state = _format_agent_state(
        state, step_idx, config, max_steps=max_steps
    )
    has_preview = bool(assembled_preview and group_map)
    dispatch_available = any(a.kind == ActionKind.DISPATCH for a in actions)
    system = _system_prompt(
        depth=depth,
        dispatch_available=dispatch_available,
        has_preview=has_preview,
    )

    reports_block = ""
    if state.reports_context:
        reports_block = (
            f"\n=== Subagent Reports ===\n{state.reports_context}\n"
            f"=== End Subagent Reports ===\n"
        )
    preview_block = ""
    if has_preview:
        preview_block = (
            f"\n=== Assembled Evidence (rank these on FINISH) ===\n"
            f"{assembled_preview}\n"
            f"=== End Assembled Evidence ===\n"
        )

    focus_need = str(getattr(state, "focus_subgoal_need", "") or "").strip()
    focus_rq = str(getattr(state, "focus_retrieval_query", "") or "").strip()
    effective_query = focus_rq or state.query
    user = (
        f"User query: {effective_query}\n"
        f"Task type: {state.task_type}\n\n"
        f"{agent_state}\n\n"
        f"=== Actionable Observation ===\n"
        f"{projection.text}\n"
        f"=== End Actionable Observation ===\n"
        f"{reports_block}"
        f"{preview_block}\n"
        'Return: {"action_id":"...","reason":"..."}'
    )
    if focus_need:
        focus_id = str(getattr(state, "focus_subgoal_id", "") or "").strip()
        focus_contract = str(getattr(state, "focus_subgoal_contract", "") or "").strip()
        episode_line = ""
        if focus_rq and focus_rq.strip() != str(state.query or "").strip():
            episode_line = f"episode_query: {state.query}\n"
        focus_block = (
            f"\n=== Current Subgoal (soft focus; action space unchanged) ===\n"
            f"id: {focus_id or '-'}\n"
            f"{episode_line}"
            f"need: {focus_need}\n"
            f"retrieval_query: {focus_rq or focus_need}\n"
            f"contract: {focus_contract or '-'}\n"
            f"Prefer evidence that serves this need, but you may still collect any "
            f"visible useful node.\n"
            f"=== End Current Subgoal ===\n"
        )
        user = (
            f"User query: {effective_query}\n"
            f"Task type: {state.task_type}\n\n"
            f"{agent_state}\n"
            f"{focus_block}\n"
            f"=== Actionable Observation ===\n"
            f"{projection.text}\n"
            f"=== End Actionable Observation ===\n"
            f"{reports_block}"
            f"{preview_block}\n"
            'Return: {"action_id":"...","reason":"..."}'
        )

    purpose = "nav_navigate_v1" if depth == 0 else "nav_subagent_v1"
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            cached = nav_chat(
                purpose=purpose,
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=float(config.llm_temperature),
                max_tokens=int(config.llm_max_tokens),
                response_format={"type": "json_object"},
                context="Nav Agent",
                usage_tag="nav",
            )
            text = str(cached.get("content") or "").strip()
            obj = _extract_json_obj(text) or {}
            aid = str(obj.get("action_id") or obj.get("id") or "").strip().upper()
            if not aid:
                aid = _extract_action_id_fallback(text)
            primary = action_by_id(actions, aid)
            if primary is not None:
                meta: dict[str, Any] = {
                    "model": model,
                    "reason": str(obj.get("reason") or "")[:300],
                    "raw": text[:500],
                    "depth": depth,
                }
                if primary.kind in {ActionKind.COLLECT, ActionKind.DISPATCH}:
                    id_list = _normalize_id_list(obj, primary.action_id.upper())
                    selected = actions_by_ids(actions, id_list)
                    selected = [a for a in selected if a.kind == primary.kind]
                    if not selected:
                        selected = [primary]
                    meta["selected_ids"] = [a.action_id for a in selected]
                    meta["selected_section_ids"] = [a.section_id for a in selected]
                    primary.metadata = dict(primary.metadata or {})
                    primary.metadata["batch_actions"] = selected
                    if primary.kind == ActionKind.COLLECT:
                        from .nav_compose import parse_collect_confidence

                        conf_by_aid = parse_collect_confidence(obj, selected)
                        conf_by_sid = {
                            str(a.section_id): float(conf_by_aid.get(a.action_id.upper(), 0.0))
                            for a in selected
                            if a.section_id
                        }
                        meta["confidence_by_action"] = conf_by_aid
                        meta["confidence_by_section"] = conf_by_sid
                        primary.metadata["confidence_by_section"] = conf_by_sid
                if primary.kind == ActionKind.FINISH and group_map:
                    rank_raw = obj.get("group_rank") or obj.get("groups") or []
                    if isinstance(rank_raw, list) and rank_raw:
                        n = len(rank_raw)
                        applied: List[str] = []
                        for i, g in enumerate(rank_raw):
                            gid = str(g).strip().upper()
                            pid = group_map.get(gid)
                            if pid:
                                state.group_priority[pid] = float(n - i)
                                applied.append(gid)
                        if applied:
                            meta["group_rank"] = applied
                return primary, meta
            last_error = RuntimeError(
                "Nav Agent LLM 返回了非法 action_id="
                f"{aid!r}；合法选项={[a.action_id for a in actions]!r}；raw={text[:500]!r}"
            )
            if attempt < 2:
                time.sleep(min(2.0, 0.4 * (attempt + 1)))
                continue
            # Do not silently COLLECT the first tree row — finish this scope.
            fallback = _finish_or_rule(
                state, projection, actions, step_idx=step_idx, config=config
            )
            state.refusal_events.append(
                {
                    "tool": "policy",
                    "status": "illegal_action",
                    "message": (
                        f"illegal action_id={aid!r} after retries; "
                        f"finishing scope with {fallback.action_id}"
                    ),
                    "illegal_action_id": aid,
                    "fallback_action_id": fallback.action_id,
                    "fallback_kind": fallback.kind.value,
                    "depth": depth,
                    "step_idx": step_idx,
                }
            )
            return fallback, {
                "model": model,
                "reason": "illegal_action_finish",
                "raw": text[:500],
                "illegal_action_id": aid,
                "fallback_action_id": fallback.action_id,
                "depth": depth,
            }
        except NavTokenLimit:
            return _token_limit_finish()
        except RuntimeError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(min(2.0, 0.4 * (attempt + 1)))
                continue
            raise
        except Exception as exc:
            last_error = exc
            time.sleep(min(2.0, 0.4 * (attempt + 1)))
    raise RuntimeError(
        f"Nav Agent LLM 调用失败（model={model!r}，step={step_idx}）：{last_error}"
    )
