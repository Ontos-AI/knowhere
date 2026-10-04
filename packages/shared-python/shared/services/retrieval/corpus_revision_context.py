"""Carry immutable revision pins across asynchronous harness/tool callbacks."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import case
from sqlalchemy.sql.elements import ColumnElement

from shared.models.database.document import Document
from shared.models.database.demo_corpus import DemoDocument

_CAPTURED_REVISIONS: ContextVar[Mapping[str, str] | None] = ContextVar(
    "retrieval_corpus_revisions", default=None
)


class CorpusRevisionContext:
    @staticmethod
    @contextmanager
    def bind(revisions: Mapping[str, str]) -> Iterator[None]:
        token = _CAPTURED_REVISIONS.set(revisions)
        try:
            yield
        finally:
            _CAPTURED_REVISIONS.reset(token)

    @staticmethod
    def get_pins() -> Mapping[str, str] | None:
        return _CAPTURED_REVISIONS.get()

    @staticmethod
    def resolve_revision(document_id: str, current_revision: str | None) -> str | None:
        revisions: Mapping[str, str] | None = _CAPTURED_REVISIONS.get()
        return current_revision if revisions is None else revisions.get(document_id)

    @staticmethod
    def build_revision_column(
        model: type[Document] | type[DemoDocument],
    ) -> ColumnElement[str | None]:
        revisions: Mapping[str, str] | None = _CAPTURED_REVISIONS.get()
        if revisions is None:
            return model.current_job_result_id.__clause_element__()
        return case(dict(revisions), value=model.document_id, else_=None)
