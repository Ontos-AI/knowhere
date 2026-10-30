"""Publication-time persistence and same-revision rebinding of page embeddings."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pgvector import HalfVector
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Delete

from shared.core.config import settings
from shared.services.ai.embedding import embed_query, resolve_embedding_model
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.publication_models import DocumentPublicationScope

__all__ = [
    "extract_and_strip_page_embeddings",
    "inherit_missing_page_assets",
    "load_revision_page_assets_by_page",
    "load_missing_document_page_embedding_pages",
    "query_visual_candidate_pages",
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
        dict(page_assets_by_page[page]) for page in pages if page in page_assets_by_page
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
        .where(corpusStorage.DocumentChunk.user_id == scope.user_id)
        .where(corpusStorage.DocumentChunk.namespace == scope.namespace)
        .where(corpusStorage.DocumentChunk.document_id == scope.document_id)
        .where(corpusStorage.DocumentChunk.job_result_id == scope.job_result_id)
        .where(corpusStorage.DocumentChunk.chunk_type == "page")
        .order_by(
            corpusStorage.DocumentChunk.sort_order,
            corpusStorage.DocumentChunk.id,
        )
    ).all()
    owner_by_page: dict[int, str] = {}
    start_owner_pages: set[int] = set()
    for chunk_id, metadata in rows:
        if not chunk_id or not isinstance(metadata, dict):
            continue
        pages = _coerce_page_nums(metadata.get("page_nums"))
        for index, page_num in enumerate(pages):
            cid = str(chunk_id)
            if page_num not in owner_by_page:
                owner_by_page[page_num] = cid
                if index == 0:
                    start_owner_pages.add(page_num)
            elif index == 0 and page_num not in start_owner_pages:
                owner_by_page[page_num] = cid
                start_owner_pages.add(page_num)
    return owner_by_page


def _remove_orphan_page_embeddings(
    db: Session,
    *,
    scope: DocumentPublicationScope,
    owner_by_page: dict[int, str],
) -> None:
    """Remove vectors whose physical pages lost their published chunk owner."""
    corpus_storage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
    statement: Delete = (
        delete(corpus_storage.DocumentPageEmbedding)
        .where(corpus_storage.DocumentPageEmbedding.user_id == scope.user_id)
        .where(corpus_storage.DocumentPageEmbedding.namespace == scope.namespace)
        .where(corpus_storage.DocumentPageEmbedding.document_id == scope.document_id)
        .where(
            corpus_storage.DocumentPageEmbedding.job_result_id == scope.job_result_id
        )
    )
    if owner_by_page:
        statement = statement.where(
            corpus_storage.DocumentPageEmbedding.page_num.not_in(tuple(owner_by_page))
        )
    db.execute(statement)


def load_missing_document_page_embedding_pages(
    db: Session,
    *,
    scope: DocumentPublicationScope,
) -> list[int]:
    """Return published owner pages still missing this scope's visual vectors.

    Physical pages intentionally omitted from the current published chunks are
    outside repair coverage. Publication callers hold the document lock while
    changing owners; recovery recomputes coverage again after provider work.
    """
    owner_by_page: dict[int, str] = _build_owner_chunk_by_page(db, scope=scope)
    if not owner_by_page:
        return []
    corpus_storage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
    persisted_pages: set[int] = set(
        db.scalars(
            select(corpus_storage.DocumentPageEmbedding.page_num)
            .where(corpus_storage.DocumentPageEmbedding.user_id == scope.user_id)
            .where(corpus_storage.DocumentPageEmbedding.namespace == scope.namespace)
            .where(
                corpus_storage.DocumentPageEmbedding.document_id == scope.document_id
            )
            .where(
                corpus_storage.DocumentPageEmbedding.job_result_id
                == scope.job_result_id
            )
        ).all()
    )
    return sorted(set(owner_by_page) - persisted_pages)


def replace_document_page_embeddings(
    db: Session,
    *,
    scope: DocumentPublicationScope,
    incoming_embeddings_by_page: dict[int, list[float]] | None = None,
) -> int:
    """Persist new M0 page embeddings, or rebind existing ones to the current revision's page chunks."""
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
    owner_by_page = _build_owner_chunk_by_page(db, scope=scope)
    if incoming_embeddings_by_page:
        model_name = resolve_embedding_model(getattr(settings, "EMBEDDING_MODEL", None))
        db.execute(
            delete(corpusStorage.DocumentPageEmbedding)
            .where(corpusStorage.DocumentPageEmbedding.user_id == scope.user_id)
            .where(corpusStorage.DocumentPageEmbedding.namespace == scope.namespace)
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

    _remove_orphan_page_embeddings(db, scope=scope, owner_by_page=owner_by_page)
    if not owner_by_page:
        return 0
    current_rows = list(
        db.scalars(
            select(corpusStorage.DocumentPageEmbedding)
            .where(corpusStorage.DocumentPageEmbedding.user_id == scope.user_id)
            .where(corpusStorage.DocumentPageEmbedding.namespace == scope.namespace)
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


def upsert_document_page_embeddings(
    db: Session,
    *,
    scope: DocumentPublicationScope,
    incoming_embeddings_by_page: dict[int, list[float]],
) -> int:
    """Repair supplied pages while retaining other vectors and rebinding all owners.

    The caller serializes this operation with revision publication by holding
    the document lock. A partial repair must never use revision replacement.
    """
    corpus_storage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
    owner_by_page: dict[int, str] = _build_owner_chunk_by_page(db, scope=scope)
    _remove_orphan_page_embeddings(db, scope=scope, owner_by_page=owner_by_page)
    if not owner_by_page:
        return 0
    rows = list(
        db.scalars(
            select(corpus_storage.DocumentPageEmbedding)
            .where(corpus_storage.DocumentPageEmbedding.user_id == scope.user_id)
            .where(corpus_storage.DocumentPageEmbedding.namespace == scope.namespace)
            .where(
                corpus_storage.DocumentPageEmbedding.document_id == scope.document_id
            )
            .where(
                corpus_storage.DocumentPageEmbedding.job_result_id
                == scope.job_result_id
            )
        ).all()
    )
    rows_by_page = {int(row.page_num): row for row in rows}
    model_name: str = resolve_embedding_model(
        getattr(settings, "EMBEDDING_MODEL", None)
    )
    repaired: int = 0
    for page_num, vector in incoming_embeddings_by_page.items():
        chunk_id: str | None = owner_by_page.get(page_num)
        if not chunk_id or not vector:
            continue
        row = rows_by_page.get(page_num)
        if row is None:
            row = corpus_storage.DocumentPageEmbedding(
                id=f"dpe_{uuid4().hex[:12]}",
                user_id=scope.user_id,
                namespace=scope.namespace,
                document_id=scope.document_id,
                job_result_id=scope.job_result_id,
                page_num=page_num,
                modality="visual",
                chunk_id=chunk_id,
                model=model_name,
                dims=len(vector),
                embedding=vector,
            )
            db.add(row)
        else:
            row.embedding = vector
            row.dims = len(vector)
            row.model = model_name
        repaired += 1
    for row in rows:
        owner: str | None = owner_by_page.get(int(row.page_num))
        if owner:
            row.chunk_id = owner
    db.flush()
    return repaired


def query_visual_candidate_pages(
    db: Session,
    *,
    namespace: str,
    document_id: str,
    job_result_id: str,
    queries: list[str],
    per_keyword_top_k: int = 3,
) -> set[int]:
    """Union of per-keyword visual nearest pages for one published revision."""
    if not bool(getattr(settings, "VISUAL_RETRIEVAL_ENABLED", True)):
        return set()
    if not queries:
        return set()

    embedding_cls = CorpusStorage.resolve_namespace(namespace).DocumentPageEmbedding
    pages: set[int] = set()
    for query in queries:
        vector = embed_query(query, usage_task="page_memory.toc_visual_query")
        rows = db.scalars(
            select(embedding_cls.page_num)
            .where(embedding_cls.document_id == document_id)
            .where(embedding_cls.job_result_id == job_result_id)
            .order_by(embedding_cls.embedding.cosine_distance(HalfVector(vector)))
            .limit(per_keyword_top_k)
        ).all()
        pages.update(int(page) for page in rows)
    return pages
