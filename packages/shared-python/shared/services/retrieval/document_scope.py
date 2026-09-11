"""Request-owned document boundary shared by retrieval and corpus tools."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, true


@dataclass(frozen=True)
class DocumentScope:
    include: frozenset[str] | None = None
    exclude: frozenset[str] = frozenset()

    def allows(self, document_id: str | None) -> bool:
        return document_id not in self.exclude and (
            self.include is None or document_id in self.include
        )

    def predicate(self, column: Any) -> Any:
        clauses = []
        if self.include is not None:
            clauses.append(column.in_(sorted(self.include)))
        if self.exclude:
            clauses.append(column.notin_(sorted(self.exclude)))
        return and_(*clauses) if clauses else true()

    def sql(self, column: str = "d.document_id") -> tuple[str, dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if self.include is not None:
            clauses.append(f"AND {column} = ANY(:scope_include)")
            params["scope_include"] = sorted(self.include)
        if self.exclude:
            clauses.append(f"AND {column} <> ALL(:scope_exclude)")
            params["scope_exclude"] = sorted(self.exclude)
        return " ".join(clauses), params

    def narrow(self, document_ids: list[str]) -> "DocumentScope":
        include = frozenset(document_ids)
        return DocumentScope(
            include if self.include is None else self.include & include, self.exclude
        )

    def excluding(self, document_ids: list[str]) -> "DocumentScope":
        """Retained exclude-only callers can only further restrict a scope."""
        return DocumentScope(self.include, self.exclude | frozenset(document_ids))
