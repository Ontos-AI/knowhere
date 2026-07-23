"""Build a deterministic, navigation-only tree from normalized title blocks."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.services.codex_export.schema import (
    DocumentBlock,
    DocumentTree,
    DocumentTreeNode,
    ExtractionFinding,
    deterministic_id,
)


_PERIPHERAL_TYPES = {"page_header", "page_footer", "page_number"}


def _heading_level(block: DocumentBlock) -> tuple[int, bool]:
    value = block.structured_content.get("level", 1)
    if isinstance(value, int) and value > 0:
        return value, False
    return 1, True


def _hierarchy_finding(
    *,
    document_id: str,
    block: DocumentBlock | None,
    message: str,
) -> ExtractionFinding:
    block_id = block.block_id if block else None
    page_number = None
    if block is not None:
        candidate = block.source_locator.get("page_number")
        page_number = candidate if isinstance(candidate, int) else None
    return ExtractionFinding(
        finding_id=deterministic_id(
            "ext", document_id, block_id or "root", "hierarchy", message
        ),
        severity="warning",
        category="hierarchy",
        message=message,
        document_id=document_id,
        block_id=block_id,
        page_number=page_number,
        native_verification_required=False,
    )


def _section_for_stack(stack: list[tuple[DocumentTreeNode, list[str]]]) -> dict[str, Any]:
    if not stack:
        return {"node_id": "sec_root", "path": [], "heading_level": 0}
    node, path = stack[-1]
    return {
        "node_id": node.node_id,
        "path": list(path),
        "heading_level": node.level,
    }


def _assign_page_ranges(
    nodes: Sequence[DocumentTreeNode],
    blocks: Sequence[DocumentBlock],
) -> None:
    for node in nodes:
        if node.start_sequence is None or node.end_sequence is None:
            continue
        pages = [
            page
            for block in blocks
            if node.start_sequence <= block.sequence <= node.end_sequence
            for page in [block.source_locator.get("page_number")]
            if isinstance(page, int)
        ]
        if pages:
            node.start_page_number = min(pages)
            node.end_page_number = max(pages)


def build_document_tree(blocks: Sequence[DocumentBlock]) -> DocumentTree:
    """Build section ownership and ranges from parser-assigned title levels."""
    ordered_blocks = sorted(blocks, key=lambda block: block.sequence)
    document_id = ordered_blocks[0].document_id if ordered_blocks else ""
    first_sequence = ordered_blocks[0].sequence if ordered_blocks else None
    last_sequence = ordered_blocks[-1].sequence if ordered_blocks else None
    root = DocumentTreeNode(
        node_id="sec_root",
        parent_node_id=None,
        title=None,
        level=0,
        title_block_id=None,
        start_sequence=first_sequence,
        end_sequence=last_sequence,
        start_page_number=None,
        end_page_number=None,
    )
    nodes = [root]
    nodes_by_id = {root.node_id: root}
    stack: list[tuple[DocumentTreeNode, list[str]]] = []
    findings: list[ExtractionFinding] = []
    heading_count = 0

    for block in ordered_blocks:
        if block.block_type == "title":
            heading_count += 1
            level, malformed_level = _heading_level(block)
            if malformed_level:
                findings.append(
                    _hierarchy_finding(
                        document_id=document_id,
                        block=block,
                        message="invalid heading level normalized to level 1",
                    )
                )
            while stack and stack[-1][0].level >= level:
                closing_node, _path = stack.pop()
                closing_node.end_sequence = block.sequence - 1

            parent_node = stack[-1][0] if stack else root
            if level > parent_node.level + 1:
                findings.append(
                    _hierarchy_finding(
                        document_id=document_id,
                        block=block,
                        message=(
                            "skipped heading level: "
                            f"parent level {parent_node.level}, child level {level}"
                        ),
                    )
                )

            parent_path = stack[-1][1] if stack else []
            path = [*parent_path, block.text]
            node_id = deterministic_id(
                "sec", document_id, *path, block.block_id
            )
            node = DocumentTreeNode(
                node_id=node_id,
                parent_node_id=parent_node.node_id,
                title=block.text,
                level=level,
                title_block_id=block.block_id,
                start_sequence=block.sequence,
                end_sequence=last_sequence,
                start_page_number=None,
                end_page_number=None,
            )
            nodes.append(node)
            nodes_by_id[node_id] = node
            parent_node.child_node_ids.append(node_id)
            stack.append((node, path))
            block.section = _section_for_stack(stack)
            continue

        if block.block_type in _PERIPHERAL_TYPES or (
            "excluded_from_section_body" in block.flags
        ):
            block.section = _section_for_stack([])
        else:
            block.section = _section_for_stack(stack)

    while stack:
        closing_node, _path = stack.pop()
        closing_node.end_sequence = last_sequence

    if heading_count == 0:
        findings.append(
            _hierarchy_finding(
                document_id=document_id,
                block=None,
                message="no headings detected",
            )
        )

    _assign_page_ranges(nodes, ordered_blocks)
    return DocumentTree(document_id=document_id, nodes=nodes, findings=findings)
