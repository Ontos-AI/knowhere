"""Contract coverage for lazy MAP-NAV tree traversal."""

from __future__ import annotations

from shared.services.retrieval.nav.nav_hierarchy import NodeMeta, ProviderToolSpace
from shared.services.retrieval.nav.nav_knowhere import KnowhereProvider, SectionRow
from shared.services.retrieval.nav.nav_map_scores import _walk_tree
from shared.services.retrieval.nav._compat import Chunk
from shared.services.retrieval.nav.nav_compose import pack_nav_evidence
from shared.services.retrieval.nav.nav_types import NavConfig, NavState


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


def test_evidence_pack_reads_titles_without_materializing_subtree_metadata() -> None:
    class _CountingMetadataProvider(_MetadataForbiddenProvider):
        metadata_calls = 0

        def node_meta(self, section_id: str) -> NodeMeta:
            self.metadata_calls += 1
            return super().node_meta(section_id)

    provider = _CountingMetadataProvider(
        doc_id="doc",
        sections=[
            SectionRow("root", None, "Root", "Root", 0, "", 0),
            SectionRow("child", "root", "Root / Child", "Child", 1, "", 1),
        ],
        units=(),
    )
    toolspace = ProviderToolSpace(provider)
    state = NavState(doc_id="doc", query="child")
    chunk = Chunk(
        node_id="child",
        doc_id="doc",
        text="evidence",
        line_ids=(1,),
        section_id="child",
    )

    result = pack_nav_evidence(
        [(chunk, 1.0)],
        toolspace,
        state,
        NavConfig(),
        budget_chars=100,
    )

    assert result.evidence_text == "[E1]\n[§ Child]\nevidence"
    assert provider.metadata_calls == 0


def test_evidence_pack_identifies_header_owners_from_parent_chain() -> None:
    provider = KnowhereProvider(
        doc_id="doc",
        sections=[
            SectionRow("root", None, "Root", "Root", 0, "", 0),
            SectionRow("parent", "root", "Root / Parent", "Parent", 1, "", 1),
            SectionRow(
                "child", "parent", "Root / Parent / Child", "Child", 2, "", 2
            ),
        ],
        units=(),
    )
    toolspace = ProviderToolSpace(provider)
    state = NavState(doc_id="doc", query="child")
    chunks = [
        Chunk("parent", "doc", "parent evidence", (1,), "parent"),
        Chunk("child", "doc", "child evidence", (2,), "child"),
    ]

    result = pack_nav_evidence(
        [(chunks[0], 1.0), (chunks[1], 0.9)],
        toolspace,
        state,
        NavConfig(),
        budget_chars=200,
    )

    assert result.kept_chunks == [chunks[1]]
    assert result.evidence_text == "[E1]\n[§ Child]\nchild evidence"
