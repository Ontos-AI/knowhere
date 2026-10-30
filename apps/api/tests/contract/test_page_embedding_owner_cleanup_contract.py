"""Page-vector ownership survives coarse replacement and partial repair."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import JSON, Integer, String, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import NullPool

from shared.services.retrieval import page_embedding_index as index
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)


class ContractBase(DeclarativeBase):
    pass


class ContractChunk(ContractBase):
    __tablename__ = "page_embedding_owner_contract_chunks"
    __table_args__ = {"prefixes": ["TEMPORARY"], "postgresql_on_commit": "DROP"}
    id: Mapped[str] = mapped_column(String, primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String)
    user_id: Mapped[str] = mapped_column(String)
    namespace: Mapped[str] = mapped_column(String)
    document_id: Mapped[str] = mapped_column(String)
    job_result_id: Mapped[str] = mapped_column(String)
    chunk_type: Mapped[str] = mapped_column(String)
    chunk_metadata: Mapped[dict[str, object]] = mapped_column(JSON)
    sort_order: Mapped[int] = mapped_column(Integer)


class ContractEmbedding(ContractBase):
    __tablename__ = "page_embedding_owner_contract_vectors"
    __table_args__ = {"prefixes": ["TEMPORARY"], "postgresql_on_commit": "DROP"}
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String)
    namespace: Mapped[str] = mapped_column(String)
    document_id: Mapped[str] = mapped_column(String)
    job_result_id: Mapped[str] = mapped_column(String)
    chunk_id: Mapped[str] = mapped_column(String)
    page_num: Mapped[int] = mapped_column(Integer)
    modality: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    dims: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(HALFVEC(3072))


@pytest.fixture
def database(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> Iterator[Session]:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    address = make_url(get_contract_database_url(postgresql_proc))
    monkeypatch.setattr(
        index.CorpusStorage,
        "resolve_namespace",
        lambda _: SimpleNamespace(
            DocumentChunk=ContractChunk, DocumentPageEmbedding=ContractEmbedding
        ),
    )
    monkeypatch.setattr(
        index, "resolve_embedding_model", lambda _: "contract-repair-model"
    )
    engine = create_engine(
        address.set(drivername="postgresql+psycopg2"), poolclass=NullPool
    )
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET LOCAL statement_timeout='5s'"))
                ContractBase.metadata.create_all(connection)
                with Session(bind=connection) as session:
                    yield session
            finally:
                if transaction.is_active:
                    transaction.rollback()
    finally:
        engine.dispose()


def seed_vectors(database: Session, with_owners: bool) -> DocumentPublicationScope:
    scope = DocumentPublicationScope(
        user_id="contract-owner",
        namespace="contract-owned",
        document_id="contract-document",
        job_result_id="contract-revision",
        source_file_name="contract.pdf",
    )
    if with_owners:
        for page in (1, 3):
            database.add(
                ContractChunk(
                    id=f"chunk-{page}",
                    chunk_id=f"new-page-{page}",
                    document_id=scope.document_id,
                    job_result_id=scope.job_result_id,
                    user_id=scope.user_id,
                    namespace=scope.namespace,
                    chunk_type="page",
                    chunk_metadata={"page_nums": [page]},
                    sort_order=page,
                )
            )
    database.add_all(
        [
            ContractChunk(
                id="foreign-owner",
                chunk_id="foreign-owner-page-2",
                user_id="other-owner",
                namespace=scope.namespace,
                document_id=scope.document_id,
                job_result_id=scope.job_result_id,
                chunk_type="page",
                chunk_metadata={"page_nums": [2]},
                sort_order=2,
            ),
            ContractChunk(
                id="foreign-namespace",
                chunk_id="foreign-namespace-page-8",
                user_id=scope.user_id,
                namespace="other-namespace",
                document_id=scope.document_id,
                job_result_id=scope.job_result_id,
                chunk_type="page",
                chunk_metadata={"page_nums": [8]},
                sort_order=8,
            ),
        ]
    )
    specifications = [
        (
            "valid",
            1,
            scope.user_id,
            scope.namespace,
            scope.document_id,
            scope.job_result_id,
        ),
        (
            "orphan",
            2,
            scope.user_id,
            scope.namespace,
            scope.document_id,
            scope.job_result_id,
        ),
        (
            "other-document",
            4,
            scope.user_id,
            scope.namespace,
            "other-document",
            scope.job_result_id,
        ),
        (
            "other-revision",
            5,
            scope.user_id,
            scope.namespace,
            scope.document_id,
            "other-revision",
        ),
        (
            "other-namespace",
            6,
            scope.user_id,
            "other-namespace",
            scope.document_id,
            scope.job_result_id,
        ),
        (
            "other-owner",
            7,
            "other-owner",
            scope.namespace,
            scope.document_id,
            scope.job_result_id,
        ),
    ]
    for identifier, page, owner, namespace, document, revision in specifications:
        database.add(
            ContractEmbedding(
                id=identifier,
                user_id=owner,
                namespace=namespace,
                document_id=document,
                job_result_id=revision,
                chunk_id=f"old-page-{page}",
                page_num=page,
                modality="visual",
                model="contract-original-model",
                dims=3072,
                embedding=[float(page) / 10] * 3072,
            )
        )
    database.flush()
    return scope


def fingerprints(database: Session) -> dict[str, tuple[str, int, str]]:
    rows = database.execute(
        text(
            "SELECT id,model,dims,md5(embedding::text) FROM page_embedding_owner_contract_vectors ORDER BY id"
        )
    ).all()
    return {row[0]: (row[1], row[2], row[3]) for row in rows}


@pytest.mark.parametrize("operation", ["rebind", "partial-repair"])
def test_removed_page_owner_prunes_only_owned_orphan_vectors(
    database: Session, operation: str
) -> None:
    scope = seed_vectors(database, with_owners=True)
    before = fingerprints(database)
    if operation == "rebind":
        count = index.replace_document_page_embeddings(database, scope=scope)
    else:
        count = index.upsert_document_page_embeddings(
            database,
            scope=scope,
            incoming_embeddings_by_page={3: [0.5] * 3072, 8: [0.75] * 3072},
        )
    database.flush()
    owned = database.scalars(
        select(ContractEmbedding)
        .where(
            ContractEmbedding.user_id == scope.user_id,
            ContractEmbedding.namespace == scope.namespace,
            ContractEmbedding.document_id == scope.document_id,
            ContractEmbedding.job_result_id == scope.job_result_id,
        )
        .order_by(ContractEmbedding.page_num)
    ).all()
    assert [row.page_num for row in owned] == (
        [1] if operation == "rebind" else [1, 3]
    ), "Ownerless page 2 must no longer leave a persisted visual vector"
    assert owned[0].chunk_id == "new-page-1"
    assert count == 1
    after = fingerprints(database)
    assert after["valid"] == before["valid"], (
        "Rebinding must preserve actual vector values, model and dimensions"
    )
    for identifier in (
        "other-document",
        "other-revision",
        "other-namespace",
        "other-owner",
    ):
        assert after[identifier] == before[identifier]


def test_empty_current_owner_map_prunes_owned_vectors(database: Session) -> None:
    scope = seed_vectors(database, with_owners=False)
    before = fingerprints(database)
    index.replace_document_page_embeddings(database, scope=scope)
    database.flush()
    after = fingerprints(database)
    assert "valid" not in after and "orphan" not in after, (
        "No-owner early return must not retain stale owned vectors"
    )
    for identifier in (
        "other-document",
        "other-revision",
        "other-namespace",
        "other-owner",
    ):
        assert after[identifier] == before[identifier]


def test_empty_current_owner_map_partial_repair_cannot_use_foreign_chunks(
    database: Session,
) -> None:
    scope = seed_vectors(database, with_owners=False)
    before = fingerprints(database)
    count = index.upsert_document_page_embeddings(
        database,
        scope=scope,
        incoming_embeddings_by_page={2: [0.5] * 3072, 8: [0.75] * 3072},
    )
    database.flush()
    after = fingerprints(database)
    owned = database.scalars(
        select(ContractEmbedding).where(
            ContractEmbedding.user_id == scope.user_id,
            ContractEmbedding.namespace == scope.namespace,
            ContractEmbedding.document_id == scope.document_id,
            ContractEmbedding.job_result_id == scope.job_result_id,
        )
    ).all()
    assert owned == [] and count == 0, (
        "Foreign chunks must not keep or create owned page vectors"
    )
    for identifier in (
        "other-document",
        "other-revision",
        "other-namespace",
        "other-owner",
    ):
        assert after[identifier] == before[identifier]


def test_incoming_replacement_preserves_other_scopes_and_ignores_foreign_owners(
    database: Session,
) -> None:
    scope = seed_vectors(database, with_owners=True)
    before = fingerprints(database)
    count = index.replace_document_page_embeddings(
        database,
        scope=scope,
        incoming_embeddings_by_page={
            1: [0.25] * 3072,
            3: [0.5] * 3072,
            8: [0.75] * 3072,
        },
    )
    database.flush()
    owned = database.scalars(
        select(ContractEmbedding)
        .where(
            ContractEmbedding.user_id == scope.user_id,
            ContractEmbedding.namespace == scope.namespace,
            ContractEmbedding.document_id == scope.document_id,
            ContractEmbedding.job_result_id == scope.job_result_id,
        )
        .order_by(ContractEmbedding.page_num)
    ).all()
    assert [row.page_num for row in owned] == [1, 3] and count == 2
    assert [row.chunk_id for row in owned] == ["new-page-1", "new-page-3"]
    assert all(
        row.model == "contract-repair-model" and row.dims == 3072 for row in owned
    )
    after = fingerprints(database)
    for identifier in (
        "other-document",
        "other-revision",
        "other-namespace",
        "other-owner",
    ):
        assert after[identifier] == before[identifier]


@pytest.mark.parametrize("has_current_owners", [False, True])
def test_missing_coverage_uses_only_current_owned_chunks_and_vectors(
    database: Session,
    has_current_owners: bool,
) -> None:
    scope = seed_vectors(database, with_owners=has_current_owners)
    for identifier, owner, namespace in (
        ("foreign-owner-page-three", "other-owner", scope.namespace),
        ("foreign-namespace-page-three", scope.user_id, "other-namespace"),
    ):
        database.add(
            ContractEmbedding(
                id=identifier,
                user_id=owner,
                namespace=namespace,
                document_id=scope.document_id,
                job_result_id=scope.job_result_id,
                chunk_id="foreign-page-three",
                page_num=3,
                modality="visual",
                model="foreign-model",
                dims=3072,
                embedding=[0.25] * 3072,
            )
        )
    database.flush()
    assert index.load_missing_document_page_embedding_pages(database, scope=scope) == (
        [3] if has_current_owners else []
    ), (
        "Foreign vectors cannot satisfy owned coverage, and foreign chunks cannot create repair obligations"
    )
