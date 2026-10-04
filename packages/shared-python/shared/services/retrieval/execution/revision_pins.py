"""Capture and carry one immutable revision set through a retrieval request."""

from __future__ import annotations

from shared.services.retrieval.corpus_storage import CorpusStorage

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import select, func
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
    generationQuery = select(corpusStorage.RetrievalNamespaceGeneration.generation).where(corpusStorage.RetrievalNamespaceGeneration.user_id == corpusStorage.resolve_owner(user_id), corpusStorage.RetrievalNamespaceGeneration.namespace == namespace).scalar_subquery()
    statement = (
        select(corpusStorage.Document.document_id, corpusStorage.Document.current_job_result_id, func.coalesce(generationQuery, 0))
        .where(corpusStorage.Document.user_id == corpusStorage.resolve_owner(user_id))
        .where(corpusStorage.Document.namespace == namespace)
        .where(corpusStorage.Document.status == "active")
        .where(corpusStorage.Document.current_job_result_id.is_not(None))
        .order_by(corpusStorage.Document.document_id)
    )
    if corpusStorage.is_demo:
        statement = statement.join(corpusStorage.DocumentMapUnitIndex, (corpusStorage.DocumentMapUnitIndex.document_id == corpusStorage.Document.document_id) & (corpusStorage.DocumentMapUnitIndex.job_result_id == corpusStorage.Document.current_job_result_id))
    capturedRows = (await db.execute(statement)).all()
    generation_row = capturedRows[0][2] if capturedRows else 0
    rows = [(identity, revision) for identity, revision, _generation in capturedRows]
    revisions = {
        str(document_id): str(job_result_id)
        for document_id, job_result_id in rows
        if document_id and job_result_id
    }
    return RetrievalRevisionPins(
        revisions=revisions,
        generation=int(generation_row) if generation_row is not None else 0,
    )


async def is_revision_generation_stable(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    pins: RetrievalRevisionPins,
) -> bool:
    """Return whether the namespace generation is unchanged since capture."""
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
    try:
        result = await db.execute(
            select(corpusStorage.RetrievalNamespaceGeneration.generation)
            .where(corpusStorage.RetrievalNamespaceGeneration.user_id == corpusStorage.resolve_owner(user_id))
            .where(corpusStorage.RetrievalNamespaceGeneration.namespace == namespace)
        )
        current_generation = result.scalar_one_or_none()
    except SQLAlchemyError:
        await db.rollback()
        return True
    return int(current_generation or 0) == int(pins.generation or 0)
