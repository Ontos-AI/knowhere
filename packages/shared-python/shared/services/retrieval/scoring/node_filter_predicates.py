"""Predicate compile/match for section path/summary filters.

Extracted from the map-nav tree walker. Live consumers evaluate these
predicates against persisted section rows, not an in-memory episode tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Literal, Sequence, Tuple

MatchKind = Literal["substring", "regex"]
FilterField = Literal["path", "summary"]

_MAX_REGEX_PATTERN_LEN = 256


@dataclass(frozen=True)
class FieldPredicate:
    field: FilterField
    terms: Tuple[str, ...]
    match: MatchKind = "substring"


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
    values: dict[str, str],
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
