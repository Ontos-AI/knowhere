"""Deterministic WHERE filter over the in-memory map-nav hierarchy.

Agent-authored predicates run on ``path`` (filename + title chain via
``path_titles``) and ``summary``. Field predicates AND together; terms inside
one field OR together. No top-K and no result truncation for substring
matches. Regex is bounded by pattern length, node count, and compile/search
exceptions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Literal, Sequence, Tuple

MatchKind = Literal["substring", "regex"]
FilterField = Literal["path", "summary"]

_MAX_REGEX_PATTERN_LEN = 256
_MAX_REGEX_NODES = 100_000


@dataclass(frozen=True)
class FieldPredicate:
    field: FilterField
    terms: Tuple[str, ...]
    match: MatchKind = "substring"


@dataclass(frozen=True)
class NodeFilter:
    predicates: Tuple[FieldPredicate, ...] = ()


@dataclass
class FilterResult:
    matched_section_ids: List[str]
    matched_doc_ids: List[str]
    cardinality: int
    truncated: bool = False
    failed_predicates: List[str] = field(default_factory=list)


def field_predicate(
    field: str,
    terms: Sequence[str],
    match: str = "substring",
) -> FieldPredicate:
    key = str(field or "").strip().lower()
    if key not in {"path", "summary"}:
        raise ValueError(f"unsupported filter field: {field!r}")
    kind = str(match or "substring").strip().lower()
    if kind not in {"substring", "regex"}:
        raise ValueError(f"unsupported filter match: {match!r}")
    cleaned = tuple(str(term) for term in terms if str(term))
    return FieldPredicate(field=key, terms=cleaned, match=kind)  # type: ignore[arg-type]


def node_filter(predicates: Sequence[FieldPredicate]) -> NodeFilter:
    return NodeFilter(predicates=tuple(predicates))


def apply_node_filter(
    ts: Any,
    doc_ids: Sequence[str],
    nf: NodeFilter,
) -> FilterResult:
    """Walk the named documents and evaluate ``nf`` on every node."""
    wanted = [str(did).strip() for did in doc_ids if str(did).strip()]
    compiled, failed = _compile_predicates(nf.predicates)
    if failed:
        return FilterResult(
            matched_section_ids=[],
            matched_doc_ids=[],
            cardinality=0,
            truncated=False,
            failed_predicates=failed,
        )
    summaries = _load_summaries(ts)
    matched_sections: List[str] = []
    matched_docs: List[str] = []
    seen_sections: set[str] = set()
    seen_docs: set[str] = set()
    visited = 0
    truncated = False
    uses_regex = any(pred.match == "regex" for pred in nf.predicates)

    for doc_id in wanted:
        for sid, owner_doc, is_doc_node in _iter_doc_nodes(ts, doc_id):
            visited += 1
            if uses_regex and visited > _MAX_REGEX_NODES:
                truncated = True
                break
            path_text = _path_text(ts, sid, owner_doc)
            summary_text = "" if is_doc_node else str(summaries.get(sid) or "")
            if not is_doc_node and not summary_text:
                summary_text = _summary_fallback(ts, sid)
            values = {"path": path_text, "summary": summary_text}
            if not _node_matches(values, compiled):
                continue
            if is_doc_node:
                if owner_doc not in seen_docs:
                    seen_docs.add(owner_doc)
                    matched_docs.append(owner_doc)
                continue
            if sid in seen_sections:
                continue
            seen_sections.add(sid)
            matched_sections.append(sid)
            if owner_doc and owner_doc not in seen_docs:
                seen_docs.add(owner_doc)
                matched_docs.append(owner_doc)
        if truncated:
            break

    return FilterResult(
        matched_section_ids=matched_sections,
        matched_doc_ids=matched_docs,
        cardinality=len(matched_sections),
        truncated=truncated,
        failed_predicates=failed,
    )


def render_submap_observation(
    ts: Any,
    result: FilterResult,
    *,
    doc_ids: Sequence[str] | None = None,
) -> str:
    """Hit-count line plus every matched node (path + summary)."""
    del doc_ids
    header = f"hits={result.cardinality}"
    if result.truncated:
        header = f"{header} truncated=true"
    if result.failed_predicates:
        header = f"{header} failed_predicates={len(result.failed_predicates)}"
    if result.cardinality == 0:
        return header

    summaries = _load_summaries(ts)
    lines = [header]
    for sid in result.matched_section_ids:
        owner = _owner_document(ts, sid)
        title = _path_text(ts, sid, owner) or sid
        block = [f"{title}"]
        summary = str(summaries.get(sid) or "").strip()
        if summary:
            block.append(f"    summary: {summary}")
        lines.append("\n".join(block))
    return "\n".join(lines)


def _compile_predicates(
    predicates: Sequence[FieldPredicate],
) -> Tuple[List[Tuple[FieldPredicate, List[Any]]], List[str]]:
    compiled: List[Tuple[FieldPredicate, List[Any]]] = []
    failed: List[str] = []
    for pred in predicates:
        if pred.match != "regex":
            compiled.append((pred, []))
            continue
        patterns: List[Any] = []
        ok = True
        for term in pred.terms:
            if len(term) > _MAX_REGEX_PATTERN_LEN:
                failed.append(f"{pred.field}:regex:too_long")
                ok = False
                break
            try:
                patterns.append(re.compile(term, flags=re.IGNORECASE))
            except re.error:
                failed.append(f"{pred.field}:regex:invalid")
                ok = False
                break
        if ok:
            compiled.append((pred, patterns))
    return compiled, failed


def _node_matches(
    values: Dict[str, str],
    compiled: Sequence[Tuple[FieldPredicate, List[Any]]],
) -> bool:
    if not compiled:
        return True
    for pred, patterns in compiled:
        text = values.get(pred.field, "")
        if pred.match == "regex":
            if not patterns or not any(p.search(text or "") for p in patterns):
                return False
            continue
        haystack = (text or "").lower()
        if not pred.terms or not any(term.lower() in haystack for term in pred.terms):
            return False
    return True


def _iter_doc_nodes(ts: Any, doc_id: str) -> Iterable[Tuple[str, str, bool]]:
    yield doc_id, doc_id, True
    stack = list(_roots(ts, doc_id))
    seen: set[str] = set()
    while stack:
        sid = stack.pop(0)
        if not sid or sid in seen:
            continue
        seen.add(sid)
        yield sid, doc_id, False
        stack[0:0] = [str(child) for child in _children(ts, sid) if str(child)]


def _roots(ts: Any, doc_id: str) -> List[str]:
    fn = getattr(ts, "sections_for_doc", None)
    if callable(fn):
        return [str(sid) for sid in (fn(doc_id) or ()) if str(sid).strip()]
    provider = getattr(ts, "_provider", None)
    root_fn = getattr(provider, "roots", None) if provider is not None else None
    if callable(root_fn):
        return [str(sid) for sid in (root_fn(doc_id) or ()) if str(sid).strip()]
    return []


def _children(ts: Any, section_id: str) -> List[str]:
    fn = getattr(ts, "_provider", None)
    child_fn = getattr(fn, "children", None) if fn is not None else None
    if callable(child_fn):
        return [str(sid) for sid in (child_fn(section_id) or ()) if str(sid).strip()]
    return []


def _path_text(ts: Any, section_id: str, doc_id: str) -> str:
    fn = getattr(ts, "path_titles", None)
    if callable(fn):
        try:
            return str(fn(section_id, doc_id) or "")
        except TypeError:
            return str(fn(section_id) or "")
    return ""


def _load_summaries(ts: Any) -> Dict[str, str]:
    provider = getattr(ts, "_provider", None)
    fn = getattr(provider, "summaries", None) if provider is not None else None
    if not callable(fn):
        return {}
    raw = fn() or {}
    return {
        str(sid): str(summary or "")
        for sid, summary in raw.items()
        if str(sid).strip() and str(summary or "").strip()
    }


def _summary_fallback(ts: Any, section_id: str) -> str:
    structure_fn = getattr(ts, "get_structure", None)
    if callable(structure_fn):
        try:
            st = structure_fn(section_id) or {}
            return str(st.get("summary") or "").strip()
        except Exception:
            return ""
    return ""


def _owner_document(ts: Any, section_id: str) -> str:
    fn = getattr(ts, "owner_document", None)
    if callable(fn):
        got = fn(section_id)
        if got:
            return str(got)
    return ""
