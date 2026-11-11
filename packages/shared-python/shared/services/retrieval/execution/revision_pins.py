"""Capture and carry one immutable revision set through a retrieval request."""

from __future__ import annotations

from shared.services.retrieval.corpus_storage import CorpusStorage

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import func, select, true
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class RetrievalRevisionPins(Mapping[str, str]):
    """The active document revisions admitted to one retrieval request."""

    revisions: Mapping[str, str]
    generation: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "revisions", MappingProxyType(dict(self.revisions)))

    def __getitem__(self, document_id: str) -> str:
        return self.revisions[document_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self.revisions)

    def __len__(self) -> int:
        return len(self.revisions)


async def capture_revision_pins(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
) -> RetrievalRevisionPins:
    """Capture active document revisions in one database read transaction."""
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
    generation_query = (
        select(corpusStorage.RetrievalNamespaceGeneration.generation)
        .where(
            corpusStorage.RetrievalNamespaceGeneration.user_id
            == corpusStorage.resolve_owner(user_id),
            corpusStorage.RetrievalNamespaceGeneration.namespace == namespace,
        )
        .scalar_subquery()
    )
    active_revisions = (
        select(
            corpusStorage.Document.document_id,
            corpusStorage.Document.current_job_result_id,
        )
        .where(corpusStorage.Document.user_id == corpusStorage.resolve_owner(user_id))
        .where(corpusStorage.Document.namespace == namespace)
        .where(corpusStorage.Document.status == "active")
        .where(corpusStorage.Document.current_job_result_id.is_not(None))
    )
    if corpusStorage.is_demo:
        active_revisions = active_revisions.join(
            corpusStorage.DocumentMapUnitIndex,
            (
                corpusStorage.DocumentMapUnitIndex.document_id
                == corpusStorage.Document.document_id
            )
            & (
                corpusStorage.DocumentMapUnitIndex.job_result_id
                == corpusStorage.Document.current_job_result_id
            ),
        )
    # The generation row anchors the result even when the active scope is
    # empty, so a first publication cannot split the generation and revisions.
    generation = select(func.coalesce(generation_query, 0).label("generation")).subquery()
    scope = active_revisions.subquery()
    statement = (
        select(scope.c.document_id, scope.c.current_job_result_id, generation.c.generation)
        .select_from(generation.outerjoin(scope, true()))
        .order_by(scope.c.document_id)
    )
    captured_rows = (await db.execute(statement)).all()
    revisions = {
        str(document_id): str(job_result_id)
        for document_id, job_result_id, _generation in captured_rows
        if document_id and job_result_id
    }
    return RetrievalRevisionPins(
        revisions=revisions,
        generation=int(captured_rows[0].generation),
    )


async def is_revision_generation_stable(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    pins: RetrievalRevisionPins,
    strict: bool = False,
) -> bool:
    """Return whether the namespace generation is unchanged since capture."""
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
    try:
        result = await db.execute(
            select(corpusStorage.RetrievalNamespaceGeneration.generation)
            .where(
                corpusStorage.RetrievalNamespaceGeneration.user_id
                == corpusStorage.resolve_owner(user_id)
            )
            .where(corpusStorage.RetrievalNamespaceGeneration.namespace == namespace)
        )
        current_generation = result.scalar_one_or_none()
    except SQLAlchemyError:
        await db.rollback()
        if strict:
            raise
        return True
    return int(current_generation or 0) == int(pins.generation or 0)
