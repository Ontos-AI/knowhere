from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
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
)
from shared.services.retrieval.nav.knowhere_hybrid import (
    ScoreUnitRow,
    score_rows_hybrid_all,
    score_unit_stream_hybrid_all,
)


@dataclass
class _FakeChunkStore:
    units_by_section: dict[str, list[UnitRow]]

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


def _providers() -> tuple[ProviderToolSpace, ProviderToolSpace]:
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
    return ProviderToolSpace(eager), ProviderToolSpace(lazy)


def test_lazy_provider_preserves_score_units_and_scores() -> None:
    eager, lazy = _providers()

    assert build_score_units(eager, "doc") == build_score_units(lazy, "doc")
    assert compute_corpus_map_and_unit_scores(
        eager, doc_ids=["doc"], query="alpha retrieval"
    ) == compute_corpus_map_and_unit_scores(
        lazy, doc_ids=["doc"], query="alpha retrieval"
    )

    lazy_provider = lazy._provider
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
