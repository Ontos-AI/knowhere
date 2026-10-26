"""Publication-time persistence and same-revision rebinding of page embeddings."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from shared.core.config import settings
from shared.services.ai.embedding import resolve_embedding_model
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.publication_models import DocumentPublicationScope

__all__ = [
    "extract_and_strip_page_embeddings",
    "inherit_missing_page_assets",
    "load_revision_page_assets_by_page",
    "replace_document_page_embeddings",
]


def _coerce_page_nums(raw_page_nums: object) -> list[int]:
    if isinstance(raw_page_nums, list):
        items = raw_page_nums
    elif raw_page_nums is None:
        return []
    else:
        items = str(raw_page_nums).split(",")
    pages: list[int] = []
    for item in items:
        try:
            page = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if page > 0:
            pages.append(page)
    return pages


def extract_and_strip_page_embeddings(
    chunk_metadata: dict[str, Any],
) -> dict[int, list[float]]:
    """Pop transient ``_page_embedding`` from ``chunk_metadata`` and key by page_num."""
    raw_embedding = chunk_metadata.pop("_page_embedding", None)
    if not isinstance(raw_embedding, list) or not raw_embedding:
        return {}
    try:
        vector = [float(val) for val in raw_embedding]
    except (TypeError, ValueError):
        return {}
    pages = _coerce_page_nums(chunk_metadata.get("page_nums"))
    if not pages:
        return {}
    return {pages[0]: vector}


def load_revision_page_assets_by_page(
    db: Session,
    *,
    scope: DocumentPublicationScope,
) -> dict[int, dict[str, Any]]:
    """Load existing ``page_assets`` keyed by ``page_num`` before chunk replacement."""
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
    metadatas = db.scalars(
        select(corpusStorage.DocumentChunk.chunk_metadata)
        .where(corpusStorage.DocumentChunk.document_id == scope.document_id)
        .where(corpusStorage.DocumentChunk.job_result_id == scope.job_result_id)
        .where(corpusStorage.DocumentChunk.chunk_type == "page")
        .order_by(
            corpusStorage.DocumentChunk.sort_order,
            corpusStorage.DocumentChunk.id,
        )
    ).all()
    assets_by_page: dict[int, dict[str, Any]] = {}
    for metadata in metadatas:
        if not isinstance(metadata, dict):
            continue
        raw_assets = metadata.get("page_assets")
        if not isinstance(raw_assets, list):
            continue
        for item in raw_assets:
            if not isinstance(item, dict):
                continue
            try:
                page_num = int(item.get("page_num"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if page_num > 0 and page_num not in assets_by_page:
                assets_by_page[page_num] = dict(item)
    return assets_by_page


def inherit_missing_page_assets(
    chunk_metadata: dict[str, Any],
    page_assets_by_page: dict[int, dict[str, Any]],
) -> None:
    """Attach prior M0/coarse ``page_assets`` when a restructured page chunk lacks them."""
    if not page_assets_by_page:
        return
    existing = chunk_metadata.get("page_assets")
    if isinstance(existing, list) and existing:
        return
    pages = _coerce_page_nums(chunk_metadata.get("page_nums"))
    inherited = [
        dict(page_assets_by_page[page])
        for page in pages
        if page in page_assets_by_page
    ]
    if inherited:
        chunk_metadata["page_assets"] = inherited


def _build_owner_chunk_by_page(
    db: Session,
    *,
    scope: DocumentPublicationScope,
) -> dict[int, str]:
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
    rows = db.execute(
        select(
            corpusStorage.DocumentChunk.chunk_id,
            corpusStorage.DocumentChunk.chunk_metadata,
        )
        .where(corpusStorage.DocumentChunk.document_id == scope.document_id)
        .where(corpusStorage.DocumentChunk.job_result_id == scope.job_result_id)
        .where(corpusStorage.DocumentChunk.chunk_type == "page")
        .order_by(
            corpusStorage.DocumentChunk.sort_order,
            corpusStorage.DocumentChunk.id,
        )
    ).all()
    owner_by_page: dict[int, str] = {}
    for chunk_id, metadata in rows:
        if not chunk_id or not isinstance(metadata, dict):
            continue
        for page_num in _coerce_page_nums(metadata.get("page_nums")):
            owner_by_page.setdefault(page_num, str(chunk_id))
    return owner_by_page


def replace_document_page_embeddings(
    db: Session,
    *,
    scope: DocumentPublicationScope,
    incoming_embeddings_by_page: dict[int, list[float]] | None = None,
) -> int:
    """Persist new M0 page embeddings, or rebind existing ones to the current revision's page chunks."""
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
    owner_by_page = _build_owner_chunk_by_page(db, scope=scope)
    if not owner_by_page and not incoming_embeddings_by_page:
        return 0

    if incoming_embeddings_by_page:
        model_name = resolve_embedding_model(getattr(settings, "EMBEDDING_MODEL", None))
        db.execute(
            delete(corpusStorage.DocumentPageEmbedding)
            .where(corpusStorage.DocumentPageEmbedding.document_id == scope.document_id)
            .where(
                corpusStorage.DocumentPageEmbedding.job_result_id == scope.job_result_id
            )
        )
        inserted = 0
        for page_num in sorted(incoming_embeddings_by_page):
            vector = incoming_embeddings_by_page[page_num]
            chunk_id = owner_by_page.get(page_num)
            if not chunk_id or not vector:
                continue
            db.add(
                corpusStorage.DocumentPageEmbedding(
                    id=f"dpe_{uuid4().hex[:12]}",
                    user_id=scope.user_id,
                    namespace=scope.namespace,
                    document_id=scope.document_id,
                    job_result_id=scope.job_result_id,
                    chunk_id=chunk_id,
                    page_num=int(page_num),
                    modality="visual",
                    model=model_name,
                    dims=len(vector),
                    embedding=vector,
                )
            )
            inserted += 1
        db.flush()
        return inserted

    current_rows = list(
        db.scalars(
            select(corpusStorage.DocumentPageEmbedding)
            .where(corpusStorage.DocumentPageEmbedding.document_id == scope.document_id)
            .where(
                corpusStorage.DocumentPageEmbedding.job_result_id == scope.job_result_id
            )
            .order_by(corpusStorage.DocumentPageEmbedding.page_num)
        ).all()
    )
    for row in current_rows:
        target_chunk_id = owner_by_page.get(int(row.page_num))
        if target_chunk_id and row.chunk_id != target_chunk_id:
            row.chunk_id = target_chunk_id
    if current_rows:
        db.flush()
    return len(current_rows)
