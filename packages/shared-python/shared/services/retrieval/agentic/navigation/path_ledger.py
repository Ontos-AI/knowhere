"""Path relationship helpers for document navigation state."""
from __future__ import annotations

from collections.abc import Iterable

from shared.services.retrieval.search.lexical_text import normalize_section_path


class PathLedger:
    """Small, authoritative wrapper for section path relationships."""

    @staticmethod
    def normalize(path: str | None) -> str:
        return normalize_section_path(str(path or "").strip())

    @classmethod
    def is_ancestor(cls, ancestor: str | None, descendant: str | None) -> bool:
        ancestor_path = cls.normalize(ancestor)
        descendant_path = cls.normalize(descendant)
        if not ancestor_path or not descendant_path:
            return False
        return descendant_path.startswith(ancestor_path + " / ")

    @classmethod
    def is_same_or_descendant(cls, path: str | None, scope: str | None) -> bool:
        candidate = cls.normalize(path)
        scope_path = cls.normalize(scope)
        if not candidate or not scope_path:
            return False
        return candidate == scope_path or candidate.startswith(scope_path + " / ")

    @classmethod
    def is_covered(cls, path: str | None, covered_paths: Iterable[str]) -> bool:
        candidate = cls.normalize(path)
        if not candidate:
            return False
        return any(
            candidate == covered
            or candidate.startswith(covered + " / ")
            for covered in (cls.normalize(item) for item in covered_paths)
            if covered
        )

    @classmethod
    def back_targets(cls, current_scope: str | None) -> list[str | None]:
        scope = cls.normalize(current_scope)
        if not scope:
            return []
        parts = [part for part in scope.split(" / ") if part]
        targets: list[str | None] = [
            " / ".join(parts[:index])
            for index in range(len(parts) - 1, 0, -1)
        ]
        targets.append(None)
        return targets

    @classmethod
    def valid_back_target(cls, current_scope: str | None, target: str | None) -> bool:
        scope = cls.normalize(current_scope)
        if not scope:
            return False
        return target is None or cls.is_ancestor(target, scope)
