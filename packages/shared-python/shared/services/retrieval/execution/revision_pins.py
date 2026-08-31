"""Capture and carry one immutable revision set through a retrieval request."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document
from shared.models.database.document import RetrievalNamespaceGeneration


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
    statement = (
        select(Document.document_id, Document.current_job_result_id)
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .where(Document.status == "active")
        .where(Document.current_job_result_id.is_not(None))
        .order_by(Document.document_id)
    )
    try:
        generation_result = await db.execute(
            select(RetrievalNamespaceGeneration.generation)
            .where(RetrievalNamespaceGeneration.user_id == user_id)
            .where(RetrievalNamespaceGeneration.namespace == namespace)
        )
        generation_row = generation_result.scalar_one_or_none()
    except SQLAlchemyError:
        await db.rollback()
        generation_row = None
    rows = (await db.execute(statement)).all()
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
    try:
        result = await db.execute(
            select(RetrievalNamespaceGeneration.generation)
            .where(RetrievalNamespaceGeneration.user_id == user_id)
            .where(RetrievalNamespaceGeneration.namespace == namespace)
        )
        current_generation = result.scalar_one_or_none()
    except SQLAlchemyError:
        await db.rollback()
        return True
    return int(current_generation or 0) == int(pins.generation or 0)
