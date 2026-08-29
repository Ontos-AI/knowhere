"""Contract coverage for lazy MAP-NAV tree traversal."""

from __future__ import annotations

from shared.services.retrieval.nav.nav_hierarchy import NodeMeta, ProviderToolSpace
from shared.services.retrieval.nav.nav_knowhere import KnowhereProvider, SectionRow
from shared.services.retrieval.nav.nav_map_scores import _walk_tree


class _MetadataForbiddenProvider(KnowhereProvider):
    def node_meta(self, section_id: str) -> NodeMeta:
        raise AssertionError(f"tree traversal materialized metadata for {section_id}")


def test_tree_walk_reads_children_and_titles_without_materializing_metadata() -> None:
    provider = _MetadataForbiddenProvider(
        doc_id="doc",
        sections=[
            SectionRow("root", None, "Root", "Root", 0, "", 0),
            SectionRow("child", "root", "Root / Child", "Child", 1, "", 1),
        ],
        units=(),
    )

    children, leaves, titles = _walk_tree(
        ProviderToolSpace(provider),
        "doc",
        ["root"],
    )

    assert children == {"root": ["child"], "child": []}
    assert leaves == {"child"}
    assert titles == {"root": "Root", "child": "Child"}
