from __future__ import annotations

from typing import Any, Dict, List

from ._compat import ToolSpace
from .nav_address import owner_document
from .nav_types import LegalAction, NavConfig, NavState


def _batch_actions(chosen: LegalAction) -> List[LegalAction]:
    batch = (chosen.metadata or {}).get("batch_actions")
    if isinstance(batch, list) and batch:
        return [a for a in batch if isinstance(a, LegalAction)]
    return [chosen]


def _section_ancestor_depth(ts: ToolSpace, state: NavState, section_id: str) -> int:
    """Structural depth ≈ ancestor count (deeper nodes sort first for COLLECT)."""
    sid = str(section_id or "").strip()
    if not sid:
        return 0
    doc = owner_document(ts, sid, "") or str(state.doc_id or "")
    relations = getattr(ts, "section_relation_ids", None)
    if not callable(relations):
        relations = getattr(ts, "relations", None)
    if callable(relations) and doc:
        try:
            ancestors, _desc = relations(sid, doc)
            return len(ancestors or ())
        except Exception:
            return 0
    return 0


def _batch_collect_deepest_first(
    ts: ToolSpace, state: NavState, chosen: LegalAction
) -> List[LegalAction]:
    """Order a COLLECT batch deepest-first so parent purge after child hydrate is stable."""
    acts = [
        a
        for a in _batch_actions(chosen)
        if str(getattr(a, "section_id", "") or "").strip()
    ]
    acts.sort(
        key=lambda a: (
            -_section_ancestor_depth(ts, state, str(a.section_id)),
            str(a.section_id),
        )
    )
    return acts


def _apply_collect(
    ts: ToolSpace,
    state: NavState,
    chosen: LegalAction,
    config: NavConfig,
) -> Dict[str, Any]:
    """Run one (possibly batched) COLLECT; mutates state."""
    from .nav_agent import (  # late import avoids cycle
        _add_scored,
        _collect_subtree,
        _mark_collected_branch,
        _purge_descendant_evidence,
    )
    from .nav_compose import evidence_owner_section_id

    detail: Dict[str, Any] = {
        "kind": "collect",
        "section_id": chosen.section_id,
        "collect_section_ids": [],
        "n_added": 0,
        "n_hits": 0,
        "n_purged_descendant_evidence": 0,
    }
    total_added = 0
    total_hits = 0
    total_purged = 0
    sids: List[str] = []
    conf_by_sid = dict((chosen.metadata or {}).get("confidence_by_section") or {})
    for act in _batch_collect_deepest_first(ts, state, chosen):
        sid = str(act.section_id or "").strip()
        if not sid:
            continue
        # Explicit COLLECT root: record target + confidence from LLM (missing => 0).
        state.explicit_collect_ids.add(sid)
        conf = float(conf_by_sid.get(sid, 0.0) or 0.0)
        state.collect_confidence[sid] = max(0.0, min(1.0, conf))
        # Absorb prior child COLLECTs (e.g. L93) before parent hydrate (L92).
        total_purged += _purge_descendant_evidence(ts, state, sid)
        scored = _collect_subtree(ts, act, state, config)
        # Hydration descendants: confidence stays 0 unless previously explicit.
        for chunk, _score in scored:
            owner = evidence_owner_section_id(chunk)
            if owner and owner != sid:
                state.collect_confidence.setdefault(owner, 0.0)
        added = _add_scored(state, scored)
        cov = _mark_collected_branch(ts, act, state, added)
        total_added += added
        total_hits += len(scored)
        sids.append(sid)
        detail.update(cov)
    detail["n_added"] = total_added
    detail["n_hits"] = total_hits
    detail["n_purged_descendant_evidence"] = total_purged
    detail["collect_section_ids"] = sids
    if sids:
        detail["section_id"] = sids[0]
    return detail
