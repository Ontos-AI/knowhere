"""Map-unit order follows document structure across generated section IDs."""

from shared.services.retrieval.scoring.hierarchy import ProviderToolSpace
from shared.services.retrieval.scoring.knowhere_provider import KnowhereProvider, SectionRow
from shared.services.retrieval.scoring.score_units import build_score_units


def _build_sections(first_id: str, second_id: str) -> list[SectionRow]:
    return [
        SectionRow(first_id, None, "Zulu", "Zulu", 0, "", 0),
        SectionRow(second_id, None, "Alpha", "Alpha", 0, "", 1),
    ]


def test_map_unit_order_uses_source_order_across_generated_ids() -> None:
    orders: list[list[str]] = []
    for first_id, second_id in (("sec_z", "sec_a"), ("sec_b", "sec_y")):
        provider = KnowhereProvider(
            doc_id="document", sections=_build_sections(first_id, second_id), units=()
        )
        units = build_score_units(ProviderToolSpace(provider), "document")
        orders.append([str(unit["path_text"]) for unit in units])

    assert orders == [["Zulu", "Alpha"], ["Zulu", "Alpha"]]
