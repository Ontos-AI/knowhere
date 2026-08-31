from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
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
    PersistedBm25Stats,
    PersistedScoreCorpus,
    PersistedScoreUnit,
    score_persisted_corpus_many,
)


@dataclass
class _FakeChunkStore:
    units_by_section: dict[str, list[UnitRow]]
    section_loads: int = 0

    def load_section_units(
        self,
        document_id: str,
        section_id: str,
        extra_chunk_ids: Sequence[str] = (),
    ) -> list[UnitRow]:
        del document_id
        self.section_loads += 1
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
    eager_scores = compute_corpus_map_and_unit_scores(
        eager, doc_ids=["doc"], query="alpha retrieval"
    )
    lazy_scores = compute_corpus_map_and_unit_scores(
        lazy, doc_ids=["doc"], query="alpha retrieval"
    )
    assert eager_scores[1] == {}
    assert lazy_scores[1] == {}
    assert all(score == 0.0 for score in eager_scores[0].values())
    assert all(score == 0.0 for score in lazy_scores[0].values())

    lazy_provider = lazy._provider
    self_units = getattr(lazy_provider, "self_units")
    assert [unit.chunk_id for unit in self_units("leaf")] == [
        "duplicate-chunk",
        "asset-1",
    ]


def test_persisted_score_projection_ranks_matching_units() -> None:
    rows: list[dict[str, str]] = [
        {
            "chunk_id": "unit-a",
            "path_search_text": "root alpha",
            "content_search_text": "common alpha alpha evidence",
        },
        {
            "chunk_id": "unit-b",
            "path_search_text": "root beta",
            "content_search_text": "common beta evidence",
        },
        {
            "chunk_id": "unit-c",
            "path_search_text": "root common",
            "content_search_text": "common evidence",
        },
    ]
    queries = ["common alpha", "beta evidence"]
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
            )
            for row in rows
        ],
        path_stats=build_stats("path_search_text"),
        content_stats=build_stats("content_search_text"),
    )

    scored = score_persisted_corpus_many(corpus, queries)
    assert set(scored) == set(queries)
    assert scored["common alpha"]["unit-a"] > scored["common alpha"]["unit-b"]
    assert scored["beta evidence"]["unit-b"] > scored["beta evidence"]["unit-a"]


def test_missing_index_does_not_read_chunk_payloads() -> None:
    _eager, lazy, store = _providers()
    queries: list[str] = ["alpha retrieval", "supporting image"]
    store.section_loads = 0
    actual = compute_corpus_map_and_unit_scores_many(
        lazy,
        doc_ids=["doc"],
        queries=queries,
    )

    assert set(actual) == set(queries)
    assert all(unit_scores == {} for _map_scores, unit_scores in actual.values())
    assert store.section_loads == 0


def test_missing_index_is_empty_across_documents() -> None:
    eager, lazy, store = _multi_document_providers()
    queries: list[str] = ["alpha evidence", "beta evidence"]
    store.section_loads = 0
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
    assert all(unit_scores == {} for _map_scores, unit_scores in actual.values())
    assert store.section_loads == 0


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
