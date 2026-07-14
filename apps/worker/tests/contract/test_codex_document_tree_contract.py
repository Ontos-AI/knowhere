from __future__ import annotations

from app.services.codex_export.schema import (
    BLOCK_SCHEMA_VERSION,
    DocumentBlock,
    canonical_sha256,
)
from app.services.codex_export.tree_builder import build_document_tree


def _block(
    sequence: int,
    block_type: str,
    text: str,
    *,
    level: int | None = None,
    page: int = 1,
) -> DocumentBlock:
    structured = {"value": text}
    if level is not None:
        structured["level"] = level
    return DocumentBlock(
        schema_version=BLOCK_SCHEMA_VERSION,
        document_id="doc_tree",
        block_id=f"blk_{sequence:04d}",
        sequence=sequence,
        block_type=block_type,
        text=text,
        structured_content=structured,
        content_sha256=canonical_sha256(structured),
        section={"node_id": "sec_root", "path": [], "heading_level": 0},
        source_locator={
            "kind": "pdf_page",
            "page_index": page - 1,
            "page_number": page,
            "block_index": sequence,
        },
        assets=[],
        provenance={
            "parser": "MinerU",
            "source_artifact": "content_list_v2",
            "derivation": "parser_extracted",
            "evidence_use": "source_derivative",
            "native_verification_required": False,
        },
        flags=[],
    )


def _nodes_by_title(tree) -> dict[str, list]:
    result: dict[str, list] = {}
    for node in tree.nodes:
        if node.title is not None:
            result.setdefault(node.title, []).append(node)
    return result


def test_normal_nested_hierarchy_links_body_to_nearest_section() -> None:
    blocks = [
        _block(0, "title", "1. Scope", level=1, page=1),
        _block(1, "paragraph", "Scope body", page=1),
        _block(2, "title", "1.1 Details", level=2, page=2),
        _block(3, "paragraph", "Detail body", page=2),
        _block(4, "title", "2. Results", level=1, page=3),
        _block(5, "paragraph", "Results body", page=3),
    ]

    tree = build_document_tree(blocks)
    nodes = _nodes_by_title(tree)

    scope = nodes["1. Scope"][0]
    details = nodes["1.1 Details"][0]
    results = nodes["2. Results"][0]
    assert details.parent_node_id == scope.node_id
    assert results.parent_node_id == "sec_root"
    assert scope.child_node_ids == [details.node_id]
    assert blocks[1].section["node_id"] == scope.node_id
    assert blocks[3].section["node_id"] == details.node_id
    assert blocks[5].section["node_id"] == results.node_id


def test_skipped_heading_level_attaches_deterministically_and_records_finding() -> None:
    blocks = [
        _block(0, "title", "Root section", level=1),
        _block(1, "title", "Skipped to three", level=3),
        _block(2, "paragraph", "Body"),
    ]

    tree = build_document_tree(blocks)
    nodes = _nodes_by_title(tree)

    assert nodes["Skipped to three"][0].parent_node_id == nodes["Root section"][0].node_id
    assert any(
        finding.category == "hierarchy" and "skipped heading level" in finding.message
        for finding in tree.findings
    )


def test_repeated_title_text_creates_distinct_stable_nodes() -> None:
    blocks = [
        _block(0, "title", "Repeated", level=1),
        _block(1, "paragraph", "First"),
        _block(2, "title", "Repeated", level=1),
        _block(3, "paragraph", "Second"),
    ]

    first = build_document_tree(blocks)
    second = build_document_tree(blocks)
    first_ids = [node.node_id for node in first.nodes if node.title == "Repeated"]
    second_ids = [node.node_id for node in second.nodes if node.title == "Repeated"]

    assert len(set(first_ids)) == 2
    assert first_ids == second_ids


def test_same_title_under_different_parents_has_distinct_parentage() -> None:
    blocks = [
        _block(0, "title", "Parent A", level=1),
        _block(1, "title", "Shared", level=2),
        _block(2, "title", "Parent B", level=1),
        _block(3, "title", "Shared", level=2),
    ]

    tree = build_document_tree(blocks)
    nodes = _nodes_by_title(tree)
    shared = nodes["Shared"]

    assert shared[0].node_id != shared[1].node_id
    assert {node.parent_node_id for node in shared} == {
        nodes["Parent A"][0].node_id,
        nodes["Parent B"][0].node_id,
    }


def test_no_headings_creates_only_root_and_finding() -> None:
    blocks = [_block(0, "paragraph", "Body", page=2)]

    tree = build_document_tree(blocks)

    assert [node.node_id for node in tree.nodes] == ["sec_root"]
    assert blocks[0].section["node_id"] == "sec_root"
    assert len(tree.findings) == 1
    assert tree.findings[0].category == "hierarchy"
    assert tree.findings[0].message == "no headings detected"


def test_one_heading_has_complete_sequence_and_page_ranges() -> None:
    blocks = [
        _block(0, "title", "Only heading", level=1, page=4),
        _block(1, "paragraph", "Body one", page=4),
        _block(2, "paragraph", "Body two", page=5),
    ]

    tree = build_document_tree(blocks)
    node = _nodes_by_title(tree)["Only heading"][0]

    assert node.start_sequence == 0
    assert node.end_sequence == 2
    assert node.start_page_number == 4
    assert node.end_page_number == 5


def test_section_end_ranges_include_children_but_stop_before_next_peer() -> None:
    blocks = [
        _block(0, "title", "A", level=1, page=1),
        _block(1, "paragraph", "A body", page=1),
        _block(2, "title", "A.1", level=2, page=2),
        _block(3, "paragraph", "A.1 body", page=2),
        _block(4, "title", "B", level=1, page=3),
        _block(5, "paragraph", "B body", page=3),
    ]

    tree = build_document_tree(blocks)
    nodes = _nodes_by_title(tree)

    assert nodes["A"][0].end_sequence == 3
    assert nodes["A.1"][0].end_sequence == 3
    assert nodes["B"][0].end_sequence == 5


def test_tree_contract_is_navigation_only_and_serializable() -> None:
    tree = build_document_tree([_block(0, "title", "A", level=1)])

    payload = tree.to_dict()
    assert payload["schema_version"] == "codex-document-tree/1.0"
    assert payload["document_id"] == "doc_tree"
    assert payload["tree_origin"] == "mineru_title_levels"
    assert payload["navigation_only"] is True
    assert payload["root_node_id"] == "sec_root"
    assert "summary" not in payload["nodes"][1]

