from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence

from shared.services.retrieval.nav.nav_hierarchy import ProviderToolSpace
from shared.services.retrieval.nav.nav_knowhere import (
    KnowhereProvider,
    LazyKnowhereProvider,
    NamespaceKnowhereProvider,
    SectionRow,
    UnitRow,
)
from shared.services.retrieval.nav.nav_map_scores import (
    build_score_units,
    compute_corpus_map_and_unit_scores,
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
