from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from collections import Counter
import math
from typing import Any

from shared.services.retrieval.nav.nav_hierarchy import ProviderToolSpace
from shared.services.retrieval.nav.nav_knowhere import (
    KnowhereProvider,
    LazyKnowhereProvider,
    NamespaceKnowhereProvider,
    SectionRow,
    UnitRow,
    knowhere_database_url,
)
from shared.services.retrieval.nav.nav_map_scores import (
    build_score_units,
    compute_corpus_map_and_unit_scores,
    compute_corpus_map_and_unit_scores_many,
)
from shared.services.retrieval.nav.knowhere_hybrid import (
    ScoreUnitRow,
    PersistedBm25Stats,
    PersistedScoreCorpus,
    PersistedScoreUnit,
    score_rows_hybrid_all,
    score_persisted_corpus_many,
    score_unit_stream_hybrid_all,
    score_unit_stream_hybrid_many,
)


@dataclass
class _FakeChunkStore:
    units_by_section: dict[str, list[UnitRow]]
    document_loads: int = 0
    batch_loads: int = 0

    def load_documents_units(
        self,
        section_ids_by_document: Mapping[str, Sequence[str]],
    ) -> dict[str, list[UnitRow]]:
        self.batch_loads += 1
        return {
            document_id: [
                unit
                for section_id in section_ids
                for unit in self.units_by_section.get(section_id, ())
            ]
            for document_id, section_ids in section_ids_by_document.items()
        }

    def load_document_units(
        self,
        document_id: str,
        section_ids: Sequence[str],
        extra_chunk_ids_by_section: dict[str, Sequence[str]] | None = None,
    ) -> list[UnitRow]:
        del document_id
        self.document_loads += 1
        selected = {str(section_id) for section_id in section_ids}
        units = [
            unit
            for section_id, section_units in self.units_by_section.items()
            if section_id in selected
            for unit in section_units
        ]
        known = {unit.chunk_id for unit in units}
        for chunk_ids in (extra_chunk_ids_by_section or {}).values():
            for chunk_id in chunk_ids:
                for section_units in self.units_by_section.values():
                    for unit in section_units:
                        if unit.chunk_id == chunk_id and unit.chunk_id not in known:
                            units.append(unit)
                            known.add(unit.chunk_id)
        return units

    def load_section_units(
        self,
        document_id: str,
        section_id: str,
        extra_chunk_ids: Sequence[str] = (),
    ) -> list[UnitRow]:
        del document_id
        units = list(self.units_by_section.get(section_id, ()))
        known = {unit.chunk_id for unit in units}
        for units_in_section in self.units_by_section.values():
            for unit in units_in_section:
                if unit.chunk_id in extra_chunk_ids and unit.chunk_id not in known:
                    units.append(unit)
        return units

    def close(self) -> None:
        return None


def _providers() -> tuple[ProviderToolSpace, ProviderToolSpace, _FakeChunkStore]:
    sections = [
        SectionRow("root", None, "Root", "Root", 0, "", 0),
        SectionRow("section", "root", "Root / Section", "Section", 1, "", 1),
        SectionRow("leaf", "section", "Root / Section / Leaf", "Leaf", 2, "", 2),
    ]
    text = UnitRow(
        "duplicate-chunk",
        "leaf",
        "text",
        "alpha retrieval evidence",
        1,
        metadata={"connect_to": [{"target": "asset-1", "relation": "embeds"}]},
    )
    asset = UnitRow(
        "asset-1",
        "root",
        "image",
        "",
        2,
        file_path="images/asset.png",
        metadata={"summary": "supporting image"},
    )
    eager = NamespaceKnowhereProvider(
        [KnowhereProvider(doc_id="doc", sections=sections, units=[text, asset])],
        titles={"doc": "document"},
    )
    store = _FakeChunkStore({"root": [asset], "leaf": [text]})
    lazy = NamespaceKnowhereProvider(
        [
            LazyKnowhereProvider(
                doc_id="doc",
                sections=sections,
                chunk_store=store,
                known_chunk_ids=[text.chunk_id, asset.chunk_id],
                root_asset_ids=[asset.chunk_id],
                remounted_assets_by_section={"leaf": [asset.chunk_id]},
            )
        ],
        titles={"doc": "document"},
        chunk_owner_by_id={"duplicate-chunk": "doc", "asset-1": "doc"},
    )
    return ProviderToolSpace(eager), ProviderToolSpace(lazy), store


def _multi_document_providers() -> tuple[
    ProviderToolSpace,
    ProviderToolSpace,
    _FakeChunkStore,
]:
    first_sections = [
        SectionRow("root-a", None, "Root A", "Root A", 0, "", 0),
        SectionRow("leaf-a", "root-a", "Root A / Leaf A", "Leaf A", 1, "", 1),
    ]
    second_sections = [
        SectionRow("root-b", None, "Root B", "Root B", 0, "", 0),
        SectionRow("leaf-b", "root-b", "Root B / Leaf B", "Leaf B", 1, "", 1),
    ]
    first_unit = UnitRow("chunk-a", "leaf-a", "text", "alpha evidence", 1)
    second_unit = UnitRow("chunk-b", "leaf-b", "text", "beta evidence", 1)
    eager = NamespaceKnowhereProvider(
        [
            KnowhereProvider(
                doc_id="doc-a",
                sections=first_sections,
                units=[first_unit],
            ),
            KnowhereProvider(
                doc_id="doc-b",
                sections=second_sections,
                units=[second_unit],
            ),
        ],
        titles={"doc-a": "Document A", "doc-b": "Document B"},
    )
    store = _FakeChunkStore(
        {
            "leaf-a": [first_unit],
            "leaf-b": [second_unit],
        }
    )
    lazy = NamespaceKnowhereProvider(
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
        chunk_owner_by_id={"chunk-a": "doc-a", "chunk-b": "doc-b"},
    )
    return ProviderToolSpace(eager), ProviderToolSpace(lazy), store


def test_lazy_provider_preserves_score_units_and_scores() -> None:
    eager, lazy, store = _providers()

    assert build_score_units(eager, "doc") == build_score_units(lazy, "doc")
    assert compute_corpus_map_and_unit_scores(
        eager, doc_ids=["doc"], query="alpha retrieval"
    ) == compute_corpus_map_and_unit_scores(
        lazy, doc_ids=["doc"], query="alpha retrieval"
    )

    lazy_provider = lazy._provider
    store.document_loads = 0
    prefetch = getattr(lazy_provider, "prefetch_document_units")
    prefetch("doc")
    assert store.document_loads == 1
    self_units = getattr(lazy_provider, "self_units")
    assert [unit.chunk_id for unit in self_units("leaf")] == [
        "duplicate-chunk",
        "asset-1",
    ]


def test_streaming_scorer_preserves_exact_eager_scores() -> None:
    rows: list[ScoreUnitRow] = [
        {
            "chunk_id": "unit-a",
            "path_search_text": "root alpha",
            "content_search_text": "alpha alpha evidence",
            "term_search_text": "alpha alpha evidence root",
        },
        {
            "chunk_id": "unit-b",
            "path_search_text": "root beta",
            "content_search_text": "beta evidence",
            "term_search_text": "beta evidence root",
        },
        {
            "chunk_id": "unit-c",
            "path_search_text": "root common",
            "content_search_text": "common evidence",
            "term_search_text": "common evidence root",
        },
    ]
    eager_rows: list[dict[str, Any]] = [dict(row) for row in rows]
    eager_scores = {
        str(row["chunk_id"]): float(row["score"])
        for row in score_rows_hybrid_all(eager_rows, "alpha evidence")
    }
    replay_count: int = 0

    def unit_factory() -> Sequence[ScoreUnitRow]:
        nonlocal replay_count
        replay_count += 1
        return rows

    assert score_unit_stream_hybrid_all(unit_factory, "alpha evidence") == eager_scores
    assert replay_count == 1


def test_streaming_scorer_preserves_duplicate_id_eager_semantics() -> None:
    rows: list[ScoreUnitRow] = [
        {
            "chunk_id": "duplicate",
            "path_search_text": "alpha",
            "content_search_text": "alpha",
            "term_search_text": "alpha",
        },
        {
            "chunk_id": "duplicate",
            "path_search_text": "beta",
            "content_search_text": "beta",
            "term_search_text": "beta",
        },
        {
            "chunk_id": "other",
            "path_search_text": "alpha beta",
            "content_search_text": "alpha beta",
            "term_search_text": "alpha beta",
        },
    ]
    eager_rows: list[dict[str, Any]] = [dict(row) for row in rows]
    eager_scores = {
        str(row["chunk_id"]): float(row["score"])
        for row in score_rows_hybrid_all(eager_rows, "alpha beta")
    }

    assert score_unit_stream_hybrid_all(lambda: rows, "alpha beta") == eager_scores


def test_streaming_scorer_scores_multiple_queries_with_one_corpus_read() -> None:
    rows: list[ScoreUnitRow] = [
        {
            "chunk_id": "unit-a",
            "path_search_text": "root alpha",
            "content_search_text": "alpha alpha evidence",
            "term_search_text": "alpha alpha evidence root",
        },
        {
            "chunk_id": "unit-b",
            "path_search_text": "root beta",
            "content_search_text": "beta evidence",
            "term_search_text": "beta evidence root",
        },
        {
            "chunk_id": "unit-c",
            "path_search_text": "root common",
            "content_search_text": "common evidence",
            "term_search_text": "common evidence root",
        },
    ]
    queries: list[str] = ["alpha evidence", "beta evidence"]
    expected = {
        query: score_unit_stream_hybrid_all(lambda: rows, query) for query in queries
    }
    read_count: int = 0

    def unit_factory() -> Sequence[ScoreUnitRow]:
        nonlocal read_count
        read_count += 1
        return rows

    assert score_unit_stream_hybrid_many(unit_factory, queries) == expected
    assert read_count == 1


def test_persisted_score_projection_preserves_exact_eager_scores() -> None:
    rows: list[ScoreUnitRow] = [
        {
            "chunk_id": "unit-a",
            "path_search_text": "root alpha",
            "content_search_text": "common alpha alpha evidence",
            "term_search_text": "common alpha alpha evidence root",
        },
        {
            "chunk_id": "unit-b",
            "path_search_text": "root beta",
            "content_search_text": "common beta evidence",
            "term_search_text": "common beta evidence root",
        },
        {
            "chunk_id": "unit-c",
            "path_search_text": "root common",
            "content_search_text": "common evidence",
            "term_search_text": "common evidence root",
        },
    ]
    queries = ["common alpha", "beta evidence"]
    expected = {
        query: score_unit_stream_hybrid_all(lambda: rows, query) for query in queries
    }
    query_tokens = {token for query in queries for token in query.split()}

    def build_stats(search_field: str) -> PersistedBm25Stats:
        token_rows = [str(row[search_field]).split() for row in rows]
        document_frequency = Counter(
            token for tokens in token_rows for token in set(tokens)
        )
        document_count = len(token_rows)
        raw_idfs = [
            math.log(document_count - frequency + 0.5) - math.log(frequency + 0.5)
            for frequency in document_frequency.values()
        ]
        return PersistedBm25Stats(
            document_count=document_count,
            total_length=sum(len(tokens) for tokens in token_rows),
            document_frequency={
                token: document_frequency[token] for token in query_tokens
            },
            average_idf=sum(raw_idfs) / len(raw_idfs),
        )

    corpus = PersistedScoreCorpus(
        units=[
            PersistedScoreUnit(
                unit_id=str(row["chunk_id"]),
                path_length=len(str(row["path_search_text"]).split()),
                content_length=len(str(row["content_search_text"]).split()),
                path_frequencies={
                    token: str(row["path_search_text"]).split().count(token)
                    for token in query_tokens
                },
                content_frequencies={
                    token: str(row["content_search_text"]).split().count(token)
                    for token in query_tokens
                },
                term_scores=tuple(
                    100.0
                    if query in str(row["term_search_text"])
                    else float(
                        sum(
                            token in str(row["term_search_text"])
                            for token in query.split()
                        )
                    )
                    for query in queries
                ),
            )
            for row in rows
        ],
        path_stats=build_stats("path_search_text"),
        content_stats=build_stats("content_search_text"),
    )

    assert score_persisted_corpus_many(corpus, queries) == expected


def test_corpus_map_scores_multiple_queries_with_one_lazy_load() -> None:
    eager, lazy, store = _providers()
    queries: list[str] = ["alpha retrieval", "supporting image"]
    expected = {
        query: compute_corpus_map_and_unit_scores(
            eager,
            doc_ids=["doc"],
            query=query,
        )
        for query in queries
    }

    store.document_loads = 0
    store.batch_loads = 0
    actual = compute_corpus_map_and_unit_scores_many(
        lazy,
        doc_ids=["doc"],
        queries=queries,
    )

    assert actual == expected
    assert store.batch_loads == 1
    assert store.document_loads == 0


def test_corpus_map_batches_multiple_documents_without_score_drift() -> None:
    eager, lazy, store = _multi_document_providers()
    queries: list[str] = ["alpha evidence", "beta evidence"]
    expected = compute_corpus_map_and_unit_scores_many(
        eager,
        doc_ids=["doc-a", "doc-b"],
        queries=queries,
    )

    actual = compute_corpus_map_and_unit_scores_many(
        lazy,
        doc_ids=["doc-a", "doc-b"],
        queries=queries,
    )

    assert actual == expected
    assert store.batch_loads == 1
    assert store.document_loads == 0


def test_native_chunk_store_strips_async_driver_from_database_url(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://prod-user:prod-password@db.example/knowhere",
    )
    monkeypatch.delenv("KNOWHERE_DATABASE_URL", raising=False)

    assert (
        knowhere_database_url()
        == "postgresql://prod-user:prod-password@db.example/knowhere"
    )
