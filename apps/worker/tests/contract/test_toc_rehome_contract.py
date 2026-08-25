"""Contract tests for same-forest TOC monotonic rehome. Synthetic trees only."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.manifest import TocResult, ToolContext
from app.services.document_agent.state import ProfileBlackboard
from app.services.document_agent.structure.anchoring_primitives import (
    SkeletonAnchor,
    serialize_skeleton_anchor,
    serialize_title_node,
)
from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    TitleNode,
)
from app.services.document_agent.structure.toc_anchoring import run_toc_anchoring
from app.services.document_agent.structure.toc_rehome import rehome_forest


def _match(title: str, page: int) -> TitleMatch:
    return TitleMatch(
        page=page,
        source="anchored",
        matched_line=title,
        candidates=[page],
        evidence={},
    )


def _overrides(pages: dict[tuple[str, ...], int]) -> dict[tuple[str, ...], TitleMatch]:
    return {path: _match(path[-1], page) for path, page in pages.items()}


def _titles(nodes: list[TitleNode]) -> list[str]:
    return [node.title for node in nodes]


def test_same_parent_backjump_reorders_by_own_page() -> None:
    nodes = [
        TitleNode(title="A", level=1, printed_page=10),
        TitleNode(title="B", level=1, printed_page=20),
        TitleNode(title="C", level=1, printed_page=5),
    ]
    result = rehome_forest(
        nodes,
        _overrides({("A",): 10, ("B",): 20, ("C",): 5}),
    )
    assert _titles(result.nodes) == ["C", "A", "B"]
    assert set(result.match_overrides) == {("A",), ("B",), ("C",)}
    assert result.match_overrides[("C",)].page == 5


def test_unpaged_shell_transparent_promotes_children_and_drops_shell() -> None:
    nodes = [
        TitleNode(title="Preamble", level=1, printed_page=6),
        TitleNode(title="References", level=1, printed_page=61),
        TitleNode(
            title="List of tables",
            level=1,
            children=[
                TitleNode(title="Table 1", level=2, printed_page=7),
                TitleNode(title="Table 2", level=2, printed_page=9),
            ],
        ),
    ]
    result = rehome_forest(
        nodes,
        _overrides(
            {
                ("Preamble",): 6,
                ("References",): 61,
                ("List of tables", "Table 1"): 7,
                ("List of tables", "Table 2"): 9,
            }
        ),
    )
    assert "List of tables" not in _titles(result.nodes)
    assert _titles(result.nodes) == ["Preamble", "Table 1", "Table 2", "References"]
    assert ("List of tables",) not in result.match_overrides
    assert ("List of tables", "Table 1") not in result.match_overrides
    assert result.match_overrides[("Table 1",)].page == 7
    assert result.match_overrides[("Table 2",)].page == 9
    assert result.nodes[1].level == 1


def test_monotonic_forest_is_noop() -> None:
    nodes = [
        TitleNode(title="A", level=1, printed_page=8),
        TitleNode(title="B", level=1, printed_page=14),
        TitleNode(title="C", level=1, printed_page=20),
    ]
    overrides = _overrides({("A",): 8, ("B",): 14, ("C",): 20})
    result = rehome_forest(nodes, overrides)
    assert _titles(result.nodes) == ["A", "B", "C"]
    assert result.match_overrides == overrides
    assert result.events == []


def test_child_level_backjump_does_not_use_parent_cursor() -> None:
    nodes = [
        TitleNode(
            title="Chapter",
            level=1,
            printed_page=10,
            children=[
                TitleNode(title="Late", level=2, printed_page=30),
                TitleNode(title="Early", level=2, printed_page=12),
            ],
        ),
        TitleNode(title="Next", level=1, printed_page=40),
    ]
    result = rehome_forest(
        nodes,
        _overrides(
            {
                ("Chapter",): 10,
                ("Chapter", "Late"): 30,
                ("Chapter", "Early"): 12,
                ("Next",): 40,
            }
        ),
    )
    assert _titles(result.nodes) == ["Chapter", "Next"]
    assert _titles(result.nodes[0].children) == ["Early", "Late"]
    assert ("Chapter", "Early") in result.match_overrides
    assert ("Chapter", "Late") in result.match_overrides


def test_transparent_shell_still_rehomes_children_of_paged_node() -> None:
    """Unpaged shell expands into the parent layer; that paged node's kids still reorder."""
    nodes = [
        TitleNode(title="Chapter", level=1, printed_page=10),
        TitleNode(
            title="List",
            level=1,
            children=[
                TitleNode(
                    title="FrontMatter",
                    level=2,
                    printed_page=50,
                    children=[
                        TitleNode(title="Late", level=3, printed_page=80),
                        TitleNode(title="Early", level=3, printed_page=55),
                    ],
                ),
            ],
        ),
    ]
    result = rehome_forest(
        nodes,
        _overrides(
            {
                ("Chapter",): 10,
                ("List", "FrontMatter"): 50,
                ("List", "FrontMatter", "Late"): 80,
                ("List", "FrontMatter", "Early"): 55,
            }
        ),
    )
    assert _titles(result.nodes) == ["Chapter", "List"]
    front = result.nodes[1].children[0]
    assert front.title == "FrontMatter"
    assert _titles(front.children) == ["Early", "Late"]
    assert result.match_overrides[("List", "FrontMatter", "Early")].page == 55
    assert result.match_overrides[("List", "FrontMatter", "Late")].page == 80


def test_promote_paged_node_then_rehome_its_children() -> None:
    """Backjump promotion of a paged node still fixes inversion under that node."""
    nodes = [
        TitleNode(title="Chapter", level=1, printed_page=10),
        TitleNode(
            title="List",
            level=1,
            children=[
                TitleNode(
                    title="FrontMatter",
                    level=2,
                    printed_page=5,
                    children=[
                        TitleNode(title="Late", level=3, printed_page=8),
                        TitleNode(title="Early", level=3, printed_page=6),
                    ],
                ),
            ],
        ),
    ]
    result = rehome_forest(
        nodes,
        _overrides(
            {
                ("Chapter",): 10,
                ("List", "FrontMatter"): 5,
                ("List", "FrontMatter", "Late"): 8,
                ("List", "FrontMatter", "Early"): 6,
            }
        ),
    )
    assert "List" not in _titles(result.nodes)
    assert _titles(result.nodes) == ["FrontMatter", "Chapter"]
    assert _titles(result.nodes[0].children) == ["Early", "Late"]
    assert result.nodes[0].level == 1
    assert result.match_overrides[("FrontMatter", "Early")].page == 6
    assert result.match_overrides[("FrontMatter", "Late")].page == 8
    assert ("List",) not in result.match_overrides
    assert ("List", "FrontMatter") not in result.match_overrides


def _ctx(*, hierarchies: list[dict], page_count: int = 80) -> ToolContext:
    ctx = ToolContext(
        pdf_path="/tmp/rehome.pdf",
        job_id="rehome-test",
        blackboard=ProfileBlackboard(page_count=page_count),
        trace=None,
        settings={},
    )
    ctx.blackboard.toc_hierarchies = hierarchies
    ctx.blackboard.toc_result = TocResult(method="vlm_batch", toc_pages=[1])
    ctx.blackboard.page_full_text_cache = {
        page: "body" for page in range(1, page_count + 1)
    }
    return ctx


def test_run_toc_anchoring_single_toc_still_runs_rehome() -> None:
    hierarchies = [
        {
            "toc_range": [1, 1],
            "toc_range_unit": "page",
            "toc_with_level": [
                {"heading": "Alpha", "level": 1, "page_number": 10},
                {"heading": "Bravo", "level": 1, "page_number": 20},
                {"heading": "Charlie", "level": 1, "page_number": 5},
            ],
        }
    ]
    ctx = _ctx(hierarchies=hierarchies, page_count=30)

    def _fake_anchor(**kwargs):
        nodes = [
            TitleNode(title="Alpha", level=1, printed_page=10),
            TitleNode(title="Bravo", level=1, printed_page=20),
            TitleNode(title="Charlie", level=1, printed_page=5),
        ]
        anchor = SkeletonAnchor(
            offset=0,
            offset_status="ok",
            match_overrides=_overrides(
                {("Alpha",): 10, ("Bravo",): 20, ("Charlie",): 5}
            ),
            null_page_report=[],
            bulk_count=3,
        )
        return nodes, anchor

    with patch(
        "app.services.document_agent.calibration.orchestrator.anchor_hierarchy",
        side_effect=_fake_anchor,
    ):
        run_toc_anchoring(ctx)

    assert ctx.blackboard.skeleton_anchor is not None
    assert ctx.blackboard.skeleton_nodes is not None
    assert ctx.blackboard.pending_skeleton_anchors == []
    assert [node["title"] for node in ctx.blackboard.skeleton_nodes] == [
        "Charlie",
        "Alpha",
        "Bravo",
    ]


def test_run_toc_anchoring_rehome_before_classify_keeps_contained_graft() -> None:
    hierarchies = [
        {
            "toc_range": [1, 1],
            "toc_range_unit": "page",
            "toc_with_level": [
                {"heading": "Host", "level": 1, "page_number": 10},
                {"heading": "Tail", "level": 1, "page_number": 40},
            ],
        },
        {
            "toc_range": [5, 5],
            "toc_range_unit": "page",
            "toc_with_level": [
                {"heading": "Inner", "level": 1, "page_number": 12},
            ],
        },
    ]
    ctx = _ctx(hierarchies=hierarchies, page_count=50)

    def _fake_anchor(**kwargs):
        nodes = [
            TitleNode(title="Host", level=1, printed_page=10),
            TitleNode(title="Tail", level=1, printed_page=40),
        ]
        anchor = SkeletonAnchor(
            offset=0,
            offset_status="ok",
            match_overrides=_overrides({("Host",): 10, ("Tail",): 40}),
            null_page_report=[],
            bulk_count=2,
        )
        return nodes, anchor

    def _fake_pending(**kwargs):
        return [
            {
                "toc": hierarchies[1],
                "nodes": [
                    serialize_title_node(
                        TitleNode(title="Inner", level=1, printed_page=12)
                    )
                ],
                "skeleton_anchor": serialize_skeleton_anchor(
                    SkeletonAnchor(
                        offset=0,
                        offset_status="ok",
                        match_overrides=_overrides({("Inner",): 12}),
                        null_page_report=[],
                        bulk_count=1,
                    )
                ),
            }
        ]

    with (
        patch(
            "app.services.document_agent.calibration.orchestrator.anchor_hierarchy",
            side_effect=_fake_anchor,
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring._calibrate_pending_tocs",
            side_effect=_fake_pending,
        ),
    ):
        run_toc_anchoring(ctx)

    record = ctx.blackboard.pending_skeleton_anchors[0]
    assert record["relationship"] == "contained"
    assert record.get("grafted") is True
    host = next(
        node for node in ctx.blackboard.skeleton_nodes if node["title"] == "Host"
    )
    assert any(child["title"] == "Inner" for child in host["children"])
