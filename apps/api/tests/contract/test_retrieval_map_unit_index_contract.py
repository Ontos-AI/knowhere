from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import Engine, delete, event, select, text

from shared.models.database.document import (
    DocumentMapUnit,
    DocumentMapUnitIndex,
    DocumentMapUnitToken,
)
from shared.services.retrieval.nav.nav_hierarchy import ProviderToolSpace
from shared.services.retrieval.nav._compat import Chunk, EpisodeResult
from shared.services.retrieval.nav import nav_knowhere
from shared.services.retrieval.nav.nav_map_scores import (
    build_score_units,
    compute_corpus_map_and_unit_scores,
    select_map_highlights,
)
from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
from shared.services.retrieval.nav.nav_knowhere import (
    KnowhereProvider,
    LazyKnowhereProvider,
    NamespaceKnowhereProvider,
    ReadOnlyChunkStore,
    SectionRow,
    UnitRow,
)
from shared.services.retrieval.nav_snapshot import load_nav_snapshot
from shared.services.retrieval.publication_content import (
    replace_document_revision_content,
)
from shared.services.retrieval.nav_bridge import build_referenced_chunks
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.serving_generation import lock_namespace_generation
from tests.support.contract_database import ContractDatabase
from tests.support.retrieval_snapshot_support import contract_db_session

_USER_ID = "local-dev-user"


def test_read_only_score_loader_drives_frequency_lookup_from_token_hash(
    monkeypatch,
) -> None:
    document_id = "doc-frequency"
    job_result_id = "revision-frequency"
    executions: list[tuple[str, object]] = []

    class FakeCursor:
        def __init__(self) -> None:
            self.rows: list[tuple[object, ...]] = []

        def execute(self, statement: str, parameters: object = None) -> None:
            executions.append((statement, parameters))
            if "document_map_unit_indexes" in statement:
                self.rows = [
                    (document_id, job_result_id, 2, 1, 0.0, 0.0, 1, 1, 1, 1)
                ]
            elif "FROM document_sections" in statement:
                self.rows = [(document_id, job_result_id, 1)]
            elif "FROM document_map_units AS units" in statement:
                self.rows = [("unit-frequency", document_id, "chunk-frequency", "section-frequency", 1, 1)]
            elif "FROM document_map_unit_tokens" in statement:
                self.rows = [("unit-frequency", "path", "retrieval", 1)]
            else:
                self.rows = []

        def fetchall(self) -> list[tuple[object, ...]]:
            return self.rows

        def close(self) -> None:
            return None

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_instance = FakeCursor()

        def set_session(self, *, readonly: bool, autocommit: bool) -> None:
            assert readonly is True
            assert autocommit is True

        def cursor(self) -> FakeCursor:
            return self.cursor_instance

        def close(self) -> None:
            return None

    connection = FakeConnection()
    monkeypatch.setattr(nav_knowhere, "_connect", lambda _dsn: connection)
    store = nav_knowhere.ReadOnlyChunkStore(
        dsn="postgresql://test",
        revisions={document_id: job_result_id},
    )

    corpus = store.load_persisted_score_corpus(
        [document_id],
        {document_id: ["section-frequency"]},
        ["retrieval"],
    )

    assert corpus is not None
    selective_executions = [
        statement
        for statement, _parameters in executions
        if "SELECT DISTINCT map_unit_id" in statement
    ]
    assert len(selective_executions) == 1
    assert "JOIN (VALUES" in selective_executions[0]
    frequency_executions = [
        (statement, parameters)
        for statement, parameters in executions
        if "FROM document_map_unit_tokens" in statement
        and "SELECT DISTINCT map_unit_id" not in statement
    ]
    assert len(frequency_executions) == 1
    statement, parameters = frequency_executions[0]
    assert "scoped_units AS MATERIALIZED" in statement
    assert "JOIN scoped_units" in statement
    assert "channel = ANY" in statement
    assert "token_hash = ANY" in statement
    assert "map_unit_id = ANY" not in statement
    assert isinstance(parameters, list)
    assert parameters[0] == ["unit-frequency"]
    assert parameters[1] == ["path", "content"]
    assert parameters[2] == [
        "6e51d6a3d90b6a3243d38e6da6b3f31f49867c1360beba83da8ca9630f9672c7"
    ]


class _IncompleteIndexStore:
    """Minimal lazy store whose missing index returns no persisted scores."""

    def __init__(self, units_by_section: Mapping[str, Sequence[UnitRow]]) -> None:
        self.units_by_section = {
            str(section_id): list(units)
            for section_id, units in units_by_section.items()
        }
        self.persisted_loads = 0

    def load_persisted_score_corpus(
        self,
        document_ids: Sequence[str],
        allowed_section_ids_by_document: Mapping[str, Sequence[str]],
        queries: Sequence[str],
    ) -> None:
        del document_ids, allowed_section_ids_by_document, queries
        self.persisted_loads += 1
        return None

    def load_section_units(
        self,
        document_id: str,
        section_id: str,
        extra_chunk_ids: Sequence[str] = (),
    ) -> list[UnitRow]:
        del document_id, extra_chunk_ids
        return list(self.units_by_section.get(str(section_id), ()))

    def close(self) -> None:
        return None


async def test_published_map_units_preserve_scores_without_chunk_payload_reads(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch,
) -> None:
    identifier = uuid4().hex[:8]
    namespace = f"map-unit-index-{identifier}"
    document_id = f"doc_map_{identifier}"
    job_id = f"job_map_{identifier}"
    job_result_id = f"result_map_{identifier}"
    async with developer_api_client_factory():
        await _seed_revision(
            namespace=namespace,
            document_id=document_id,
            job_id=job_id,
            job_result_id=job_result_id,
        )
        scope = DocumentPublicationScope(
            user_id=_USER_ID,
            namespace=namespace,
            document_id=document_id,
            job_result_id=job_result_id,
            source_file_name="indexed.pdf",
        )
        chunks = [
            {
                "chunk_id": "parent-a",
                "type": "text",
                "content": "common alpha parent evidence",
                "path": "indexed.pdf/Root/Parent/intro-a",
                "order": 1,
                "metadata": {},
            },
            {
                "chunk_id": "parent-b",
                "type": "text",
                "content": "common beta parent evidence",
                "path": "indexed.pdf/Root/Parent/intro-b",
                "order": 2,
                "metadata": {},
            },
            {
                "chunk_id": "leaf-a",
                "type": "text",
                "content": "common alpha leaf evidence",
                "path": "indexed.pdf/Root/Parent/Leaf A/body",
                "order": 3,
                "metadata": {},
            },
            {
                "chunk_id": "leaf-b",
                "type": "text",
                "content": "common beta leaf evidence",
                "path": "indexed.pdf/Root/Parent/Leaf B/body",
                "order": 4,
                "metadata": {},
            },
            {
                "chunk_id": "leaf-c",
                "type": "text",
                "content": "gamma other evidence",
                "path": "indexed.pdf/Root/Parent/Leaf C/body",
                "order": 5,
                "metadata": {},
            },
        ]
        async with contract_db_session() as db:
            await db.run_sync(
                lambda sync_db: _publish_revision_with_generation_lock(
                    sync_db,
                    scope=scope,
                    chunks=chunks,
                )
            )
            await db.commit()

        async with contract_db_session() as db:
            eager_snapshot = await load_nav_snapshot(
                db,
                user_id=_USER_ID,
                namespace=namespace,
            )
        eager_toolspace = ProviderToolSpace(eager_snapshot.provider)
        expected_units = build_score_units(eager_toolspace, document_id)

        async with contract_db_session() as db:
            index = (
                await db.execute(
                    select(DocumentMapUnitIndex).where(
                        DocumentMapUnitIndex.document_id == document_id
                    )
                )
            ).scalar_one()
            persisted_units = list(
                (
                    await db.execute(
                        select(DocumentMapUnit)
                        .where(DocumentMapUnit.document_id == document_id)
                        .order_by(DocumentMapUnit.sort_order)
                    )
                ).scalars()
            )
            persisted_tokens = list(
                (
                    await db.execute(
                        select(DocumentMapUnitToken).where(
                            DocumentMapUnitToken.map_unit_id.in_(
                                [unit.id for unit in persisted_units]
                            )
                        )
                    )
                ).scalars()
            )
            index_names = {
                str(row[0])
                for row in (
                    await db.execute(
                        text(
                            "SELECT indexname FROM pg_indexes "
                            "WHERE schemaname = current_schema() "
                            "AND tablename = 'document_map_unit_tokens'"
                        )
                    )
                ).all()
            }
            lazy_snapshot = await load_nav_snapshot(
                db,
                user_id=_USER_ID,
                namespace=namespace,
                lazy=True,
            )

        assert index.unit_count == len(expected_units)
        assert index.path_document_count == sum(
            unit.path_token_count > 0 for unit in persisted_units
        )
        assert index.path_total_length == sum(
            unit.path_token_count for unit in persisted_units
        )
        assert index.content_document_count == sum(
            unit.content_token_count > 0 for unit in persisted_units
        )
        assert index.content_total_length == sum(
            unit.content_token_count for unit in persisted_units
        )
        assert [unit.unit_id for unit in persisted_units] == [
            str(unit["chunk_id"]) for unit in expected_units
        ]
        assert persisted_tokens
        assert "idx_document_map_unit_tokens_lookup" in index_names

        def reject_payload_read(
            _store: ReadOnlyChunkStore,
            _document_id: str,
            _section_id: str,
            extra_chunk_ids: Sequence[str] = (),
        ) -> list[UnitRow]:
            del extra_chunk_ids
            raise AssertionError("persisted map scoring loaded full chunk payloads")

        original_payload_loader = ReadOnlyChunkStore.load_section_units
        monkeypatch.setattr(
            ReadOnlyChunkStore,
            "load_section_units",
            reject_payload_read,
        )
        actual_scores = compute_corpus_map_and_unit_scores(
            ProviderToolSpace(lazy_snapshot.provider),
            doc_ids=[document_id],
            query="common alpha",
        )
        monkeypatch.setattr(
            ReadOnlyChunkStore,
            "load_section_units",
            original_payload_loader,
        )
        async with contract_db_session() as db:
            token_id = (
                select(DocumentMapUnitToken.id)
                .join(
                    DocumentMapUnit,
                    DocumentMapUnit.id == DocumentMapUnitToken.map_unit_id,
                )
                .where(DocumentMapUnit.document_id == document_id)
                .limit(1)
                .scalar_subquery()
            )
            await db.execute(
                delete(DocumentMapUnitToken).where(DocumentMapUnitToken.id == token_id)
            )
            await db.execute(
                delete(DocumentMapUnitIndex).where(
                    DocumentMapUnitIndex.document_id == document_id
                )
            )
            await db.commit()
            incomplete_snapshot = await load_nav_snapshot(
                db,
                user_id=_USER_ID,
                namespace=namespace,
                lazy=True,
            )
        fallback_scores = compute_corpus_map_and_unit_scores(
            ProviderToolSpace(incomplete_snapshot.provider),
            doc_ids=[document_id],
            query="common alpha",
        )
        incomplete_snapshot.close()
        lazy_snapshot.close()
        eager_snapshot.close()

    assert any(score > 0.0 for score in actual_scores[1].values())
    assert select_map_highlights(actual_scores[1], k=3)
    assert any(score > 0.0 for score in fallback_scores[1].values())
    assert any(score > 0.0 for score in fallback_scores[0].values())


async def test_lazy_snapshot_defers_selected_asset_reference_metadata(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch,
) -> None:
    identifier = uuid4().hex[:8]
    namespace = f"lazy-ref-{identifier}"
    document_id = f"doc_ref_{identifier}"
    job_id = f"job_ref_{identifier}"
    job_result_id = f"result_ref_{identifier}"
    async with developer_api_client_factory():
        await _seed_revision(
            namespace=namespace,
            document_id=document_id,
            job_id=job_id,
            job_result_id=job_result_id,
        )
        scope = DocumentPublicationScope(
            user_id=_USER_ID,
            namespace=namespace,
            document_id=document_id,
            job_result_id=job_result_id,
            source_file_name="refs.pdf",
        )
        chunks = [
            {
                "chunk_id": "body",
                "type": "text",
                "content": "body evidence",
                "path": "refs.pdf/Root/Section/body",
                "order": 1,
                "metadata": {"connect_to": [{"target": "asset"}]},
            },
            {
                "chunk_id": "asset",
                "type": "image",
                "content": "image description",
                "path": "refs.pdf/Root/image",
                "order": 2,
                "file_path": "images/asset.png",
                "metadata": {},
            },
        ]
        async with contract_db_session() as db:
            await db.run_sync(
                lambda sync_db: _publish_revision_with_generation_lock(
                    sync_db,
                    scope=scope,
                    chunks=chunks,
                )
            )
            await db.commit()

        async with contract_db_session() as db:
            persisted_units = list(
                (
                    await db.execute(
                        select(DocumentMapUnit).where(
                            DocumentMapUnit.document_id == document_id
                        )
                    )
                ).scalars()
            )
        assert any(unit.has_image for unit in persisted_units)
        assert all(not unit.has_table for unit in persisted_units)

        calls: list[tuple[str, str]] = []
        original = ReadOnlyChunkStore.load_chunk_reference_metadata

        def record_reference_load(
            store: ReadOnlyChunkStore,
            document: str,
            chunk: str,
        ) -> Mapping[str, object] | None:
            calls.append((document, chunk))
            return original(store, document, chunk)

        monkeypatch.setattr(
            ReadOnlyChunkStore,
            "load_chunk_reference_metadata",
            record_reference_load,
        )
        async with contract_db_session() as db:
            snapshot = await load_nav_snapshot(
                db,
                user_id=_USER_ID,
                namespace=namespace,
                lazy=True,
            )

        assert calls == []
        assert snapshot.chunk_ref_index[f"{document_id}:asset"]["file_path"] == (
            "images/asset.png"
        )
        assert calls == [(document_id, "asset")]
        episode = EpisodeResult(
            representation="",
            steps=[],
            scored_chunks=[
                (
                    Chunk(
                        node_id="asset",
                        doc_id=document_id,
                        text="image description",
                        line_ids=(2,),
                        section_id="root",
                    ),
                    0.75,
                )
            ],
            kept_chunks=[
                Chunk(
                    node_id="asset",
                    doc_id=document_id,
                    text="image description",
                    line_ids=(2,),
                    section_id="root",
                )
            ],
            evidence_text="image description",
            evidence_chars_actual=17,
            retrieved_nodes=["asset"],
        )
        refs, scores = build_referenced_chunks(episode, snapshot)
        assert refs == [
            {
                "chunk_id": "asset",
                "document_id": document_id,
                "chunk_type": "image",
                "section_path": "Root / image",
                "file_path": "images/asset.png",
                "job_id": job_id,
                "score": 0.75,
            }
        ]
        assert scores == {"asset": 0.75}
        snapshot.close()


async def test_connected_hydration_does_not_load_legacy_job_chunks(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    identifier = uuid4().hex[:8]
    namespace = f"connected-job-{identifier}"
    document_id = f"doc_connected_{identifier}"
    job_id = f"job_connected_{identifier}"
    job_result_id = f"result_connected_{identifier}"
    statements: list[str] = []

    def capture_job_chunk_query(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "job_chunks" in statement.lower():
            statements.append(statement)

    async with developer_api_client_factory():
        await _seed_revision(
            namespace=namespace,
            document_id=document_id,
            job_id=job_id,
            job_result_id=job_result_id,
        )
        scope = DocumentPublicationScope(
            user_id=_USER_ID,
            namespace=namespace,
            document_id=document_id,
            job_result_id=job_result_id,
            source_file_name="connected.pdf",
        )
        chunks = [
            {
                "chunk_id": "body-connected",
                "type": "text",
                "content": "body connected evidence",
                "path": "connected.pdf/Root/Section/body",
                "order": 1,
                "metadata": {"connect_to": [{"target": "asset-connected"}]},
            },
            {
                "chunk_id": "asset-connected",
                "type": "image",
                "content": "asset connected summary",
                "path": "images/asset-connected.png",
                "order": 2,
                "file_path": "images/asset-connected.png",
                "metadata": {},
            },
        ]
        async with contract_db_session() as db:
            await db.run_sync(
                lambda sync_db: _publish_revision_with_generation_lock(
                    sync_db,
                    scope=scope,
                    chunks=chunks,
                )
            )
            await db.commit()

        event.listen(Engine, "before_cursor_execute", capture_job_chunk_query)
        try:
            async with contract_db_session() as db:
                hydrated = await hydrate_connected_target_rows(
                    db=db,
                    rows=[
                        {
                            "document_id": document_id,
                            "job_result_id": job_result_id,
                            "chunk_id": "body-connected",
                            "chunk_type": "text",
                            "chunk_metadata": {
                                "connect_to": [{"target": "asset-connected"}]
                            },
                        }
                    ],
                    exclude_document_ids=[],
                    exclude_sections=[],
                    revision_pins={document_id: job_result_id},
                )
        finally:
            event.remove(Engine, "before_cursor_execute", capture_job_chunk_query)

    assert [row["chunk_id"] for row in hydrated] == ["asset-connected"]
    assert hydrated[0]["job_id"] == job_id
    assert statements == []


def test_incomplete_index_returns_empty_scores() -> None:
    first_sections = [
        SectionRow("root-a", None, "Root A", "Root A", 0, "", 0),
        SectionRow("leaf-a", "root-a", "Root A / Leaf A", "Leaf A", 1, "", 1),
    ]
    second_sections = [
        SectionRow("root-b", None, "Root B", "Root B", 0, "", 0),
        SectionRow("leaf-b", "root-b", "Root B / Leaf B", "Leaf B", 1, "", 1),
    ]
    first_unit = UnitRow("same-chunk", "leaf-a", "text", "alpha evidence", 1)
    second_unit = UnitRow("same-chunk", "leaf-b", "text", "beta evidence", 1)

    eager = ProviderToolSpace(
        NamespaceKnowhereProvider(
            [
                KnowhereProvider(
                    doc_id="doc-a", sections=first_sections, units=[first_unit]
                ),
                KnowhereProvider(
                    doc_id="doc-b", sections=second_sections, units=[second_unit]
                ),
            ],
            titles={"doc-a": "Document A", "doc-b": "Document B"},
        )
    )
    store = _IncompleteIndexStore(
        {"leaf-a": [first_unit], "leaf-b": [second_unit]}
    )
    lazy = ProviderToolSpace(
        NamespaceKnowhereProvider(
            [
                LazyKnowhereProvider(
                    doc_id="doc-a",
                    sections=first_sections,
                    chunk_store=store,
                    known_chunk_ids=[first_unit.chunk_id],
                ),
                LazyKnowhereProvider(
                    doc_id="doc-b",
                    sections=second_sections,
                    chunk_store=store,
                    known_chunk_ids=[second_unit.chunk_id],
                ),
            ],
            titles={"doc-a": "Document A", "doc-b": "Document B"},
            chunk_owner_by_id={"same-chunk": "doc-a"},
        )
    )

    expected = compute_corpus_map_and_unit_scores(
        eager, doc_ids=["doc-a", "doc-b"], query="alpha beta"
    )
    actual = compute_corpus_map_and_unit_scores(
        lazy, doc_ids=["doc-a", "doc-b"], query="alpha beta"
    )

    assert set(actual[1]) == {"leaf-a", "leaf-b"}
    assert set(expected[1]) == {"leaf-a", "leaf-b"}
    assert all(score == 0.0 for score in actual[0].values())
    assert store.persisted_loads == 1


def test_titleless_leaf_has_identical_eager_and_lazy_path_scoring() -> None:
    sections = [
        SectionRow("root", None, "Root", "Root", 0, "", 0),
        SectionRow("leaf", "root", "Root / Leaf", "", 1, "", 1),
    ]
    unit = UnitRow("titleless-chunk", "leaf", "text", "alpha evidence", 1)
    eager = ProviderToolSpace(
        KnowhereProvider(doc_id="doc", sections=sections, units=[unit])
    )
    store = _IncompleteIndexStore({"leaf": [unit]})
    lazy = ProviderToolSpace(
        LazyKnowhereProvider(
            doc_id="doc",
            sections=sections,
            chunk_store=store,
            known_chunk_ids=[unit.chunk_id],
        )
    )

    assert build_score_units(eager, "doc") == build_score_units(lazy, "doc")
    assert compute_corpus_map_and_unit_scores(
        eager, doc_ids=["doc"], query="alpha"
    ) == compute_corpus_map_and_unit_scores(
        lazy, doc_ids=["doc"], query="alpha"
    )


def _publish_revision_with_generation_lock(
    sync_db: Any,
    *,
    scope: DocumentPublicationScope,
    chunks: list[dict[str, Any]],
) -> None:
    """Mirror the production publish sequence: lock, then patch the snapshot.

    ``replace_document_revision_content`` requires the namespace generation
    row to already exist (``publication_service.py`` locks it before every
    real publish); this test helper reproduces that precondition.
    """
    lock_namespace_generation(
        sync_db, user_id=scope.user_id, namespace=scope.namespace
    )
    replace_document_revision_content(sync_db, scope=scope, chunks=chunks)


async def _seed_revision(
    *,
    namespace: str,
    document_id: str,
    job_id: str,
    job_result_id: str,
) -> None:
    await ContractDatabase.execute(
        """
        INSERT INTO jobs (
            job_id, user_id, job_type, status, source_type, version,
            webhook_enabled, created_at, updated_at, credits_charged, billing_status
        ) VALUES (
            :job_id, :user_id, 'document_ingestion', 'done', 'file', 0,
            false, NOW(), NOW(), 0, 'skipped'
        )
        """,
        {"job_id": job_id, "user_id": _USER_ID},
    )
    await ContractDatabase.execute(
        """
        INSERT INTO documents (
            document_id, user_id, namespace, status, source_file_name,
            parse_track, created_at, updated_at
        ) VALUES (
            :document_id, :user_id, :namespace, 'active',
            'indexed.pdf', 'chunk', NOW(), NOW()
        )
        """,
        {
            "document_id": document_id,
            "user_id": _USER_ID,
            "namespace": namespace,
        },
    )
    await ContractDatabase.execute(
        """
        INSERT INTO job_results (
            id, job_id, document_id, delivery_mode, created_at, updated_at
        ) VALUES (
            :job_result_id, :job_id, :document_id, 'inline', NOW(), NOW()
        )
        """,
        {
            "job_result_id": job_result_id,
            "job_id": job_id,
            "document_id": document_id,
        },
    )
    await ContractDatabase.execute(
        """
        UPDATE documents SET current_job_result_id = :job_result_id
        WHERE document_id = :document_id
        """,
        {"job_result_id": job_result_id, "document_id": document_id},
    )
