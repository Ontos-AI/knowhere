"""Readiness errors are explicit; prepared subsets remain independently usable."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.demo_not_ready import DemoNotReadyException
from shared.core.exceptions.domain_exceptions import NotFoundException
from shared.models.database.demo_corpus import DemoDocument
from shared.services.retrieval.execution.revision_pins import RetrievalRevisionPins


async def validate_demo_query_scope(
    db: AsyncSession,
    *,
    include_document_ids: list[str] | None,
    pins: RetrievalRevisionPins,
) -> None:
    if include_document_ids:
        documents = (
            await db.execute(
                select(DemoDocument.document_id, DemoDocument.status).where(
                    DemoDocument.document_id.in_(include_document_ids)
                )
            )
        ).all()
        activeIds: set[str] = {
            str(identity) for identity, status in documents if status == "active"
        }
        for identity in include_document_ids:
            if identity not in activeIds:
                raise NotFoundException(resource="Demo document", resource_id=identity)
            if identity not in pins:
                raise DemoNotReadyException(is_explicit_source=True)
    if not pins and include_document_ids != []:
        raise DemoNotReadyException(is_explicit_source=False)
