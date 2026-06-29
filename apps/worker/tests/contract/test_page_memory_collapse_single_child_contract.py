"""Contract tests for ``collapse_single_child_chains``.

Verifies the structural rule: a parent with exactly one child is merged into
``"{parent.title} {child.title}"``, the child's subtree is promoted one level
and re-parented onto the parent's ``section_path``, and chains collapse in a
single bottom-up pass. No title filtering — even generic labels merge.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.structure.hierarchy_locator import TitleNode
from app.services.page_memory.skeleton_extractor import (
    SectionSkeleton,
    collapse_single_child_chains,
    collapse_title_node_single_child_chains,
)


def _skel(
    path: str,
    *,
    title: str,
    level: int,
    parent_path: str | None,
    start_page: int = 1,
    end_page: int = 1,
    evidence: dict | None = None,
) -> SectionSkeleton:
    return SectionSkeleton(
        section_path=path,
        level=level,
        start_page=start_page,
        end_page=end_page,
        title=title,
        parent_path=parent_path,
        evidence=dict(evidence or {}),
    )


# ── Test 1: a single linear chain collapses end-to-end ───────────────


def test_linear_chain_collapses_to_single_node() -> None:
    """A → B → C (each an only child) should collapse to one node 'A B C'."""
    skeletons = [
        _skel("doc.pdf/A", title="A", level=1, parent_path="doc.pdf"),
        _skel("doc.pdf/A/B", title="B", level=2, parent_path="doc.pdf/A"),
        _skel("doc.pdf/A/B/C", title="C", level=3, parent_path="doc.pdf/A/B"),
    ]

    result = collapse_single_child_chains(skeletons)

    assert [s.title for s in result] == ["A B C"]
    assert result[0].section_path == "doc.pdf/A"
    assert result[0].level == 1
    assert result[0].parent_path == "doc.pdf"


def test_chain_collapse_accumulates_evidence() -> None:
    """Absorbed child paths are recorded in ``evidence['collapsed_from']``."""
    skeletons = [
        _skel("doc.pdf/A", title="A", level=1, parent_path="doc.pdf"),
        _skel("doc.pdf/A/B", title="B", level=2, parent_path="doc.pdf/A"),
        _skel("doc.pdf/A/B/C", title="C", level=3, parent_path="doc.pdf/A/B"),
    ]

    result = collapse_single_child_chains(skeletons)

    assert result[0].evidence["collapsed_from"] == [
        "doc.pdf/A/B",
        "doc.pdf/A/B/C",
    ]


# ── Test 2: parent with multiple children is not collapsed ───────────


def test_parent_with_multiple_children_preserves_all() -> None:
    """A parent of two children keeps both — no merge."""
    skeletons = [
        _skel("doc.pdf/A", title="A", level=1, parent_path="doc.pdf"),
        _skel("doc.pdf/A/B", title="B", level=2, parent_path="doc.pdf/A"),
        _skel("doc.pdf/A/C", title="C", level=2, parent_path="doc.pdf/A"),
    ]

    result = collapse_single_child_chains(skeletons)

    titles = sorted(s.title for s in result)
    assert titles == ["A", "B", "C"]
    # No merge happened — no collapsed_from evidence anywhere.
    assert all("collapsed_from" not in s.evidence for s in result)


def test_only_child_with_subtree_promotes_grandchildren() -> None:
    """A → B (only child) → {C, D}. A absorbs B; C and D promote to under A."""
    skeletons = [
        _skel("doc.pdf/A", title="A", level=1, parent_path="doc.pdf"),
        _skel("doc.pdf/A/B", title="B", level=2, parent_path="doc.pdf/A"),
        _skel("doc.pdf/A/B/C", title="C", level=3, parent_path="doc.pdf/A/B"),
        _skel("doc.pdf/A/B/D", title="D", level=3, parent_path="doc.pdf/A/B"),
    ]

    result = collapse_single_child_chains(skeletons)
    by_title = {s.title: s for s in result}

    # A absorbed B.
    assert "A B" in by_title
    merged = by_title["A B"]
    assert merged.section_path == "doc.pdf/A"
    assert merged.level == 1
    assert merged.evidence["collapsed_from"] == ["doc.pdf/A/B"]

    # C and D promoted one level under A.
    assert by_title["C"].section_path == "doc.pdf/A/C"
    assert by_title["C"].level == 2
    assert by_title["C"].parent_path == "doc.pdf/A"
    assert by_title["D"].section_path == "doc.pdf/A/D"
    assert by_title["D"].level == 2
    assert by_title["D"].parent_path == "doc.pdf/A"


# ── Test 3: deep chain with branching collapses only the spine ───────


def test_deep_chain_with_branch_collapses_spine_only() -> None:
    """A → B → C → {D, E}.

    - A, B, C are a single-child spine.
    - C has two children (D, E), so C is a real branching point.

    Bottom-up: C is not absorbed (it has 2 children). B's sole child is C →
    B absorbs C, becoming 'B C', and D/E promote to under B. A's sole child
    is now 'B C' → A absorbs it, becoming 'A B C', and D/E promote to under A.
    """
    skeletons = [
        _skel("doc.pdf/A", title="A", level=1, parent_path="doc.pdf"),
        _skel("doc.pdf/A/B", title="B", level=2, parent_path="doc.pdf/A"),
        _skel("doc.pdf/A/B/C", title="C", level=3, parent_path="doc.pdf/A/B"),
        _skel(
            "doc.pdf/A/B/C/D",
            title="D",
            level=4,
            parent_path="doc.pdf/A/B/C",
        ),
        _skel(
            "doc.pdf/A/B/C/E",
            title="E",
            level=4,
            parent_path="doc.pdf/A/B/C",
        ),
    ]

    result = collapse_single_child_chains(skeletons)
    by_title = {s.title: s for s in result}

    # Spine collapsed into one node; D and E are now its direct children.
    assert "A B C" in by_title
    merged = by_title["A B C"]
    assert merged.section_path == "doc.pdf/A"
    assert merged.level == 1

    # Two absorbed paths recorded in absorption order (B first, then C's
    # original path which was promoted during B's collapse).
    assert merged.evidence["collapsed_from"] == [
        "doc.pdf/A/B",
        "doc.pdf/A/B/C",
    ]

    assert by_title["D"].section_path == "doc.pdf/A/D"
    assert by_title["D"].level == 2
    assert by_title["D"].parent_path == "doc.pdf/A"
    assert by_title["E"].section_path == "doc.pdf/A/E"
    assert by_title["E"].level == 2
    assert by_title["E"].parent_path == "doc.pdf/A"


# ── Test 4: no filtering — generic labels merge too ──────────────────


def test_generic_label_merges_regardless_of_content() -> None:
    """No hardcoded filtering: a '代号型' style label merges just like any other.

    The collapse rule is purely structural. A section named like a code/number
    is still absorbed when it is the only child.
    """
    skeletons = [
        _skel(
            "doc.pdf/安全管理类",
            title="安全管理类",
            level=1,
            parent_path="doc.pdf",
        ),
        _skel(
            "doc.pdf/安全管理类/SC-001",
            title="SC-001",
            level=2,
            parent_path="doc.pdf/安全管理类",
        ),
    ]

    result = collapse_single_child_chains(skeletons)

    assert [s.title for s in result] == ["安全管理类 SC-001"]
    assert result[0].section_path == "doc.pdf/安全管理类"


# ── Test 5: result is sorted consistently ────────────────────────────


def test_result_sorted_by_start_page_level_path() -> None:
    """Output respects ``_sort_skeletons`` ordering so downstream is stable."""
    skeletons = [
        _skel(
            "doc.pdf/Z",
            title="Z",
            level=1,
            parent_path="doc.pdf",
            start_page=5,
        ),
        _skel(
            "doc.pdf/A",
            title="A",
            level=1,
            parent_path="doc.pdf",
            start_page=2,
        ),
        _skel(
            "doc.pdf/A/B",
            title="B",
            level=2,
            parent_path="doc.pdf/A",
            start_page=2,
        ),
    ]

    result = collapse_single_child_chains(skeletons)

    # A (page 2) comes before Z (page 5). A absorbed B.
    assert [s.title for s in result] == ["A B", "Z"]
    assert result[0].start_page == 2
    assert result[1].start_page == 5


# ── Test 6: section_path / parent_path self-consistency ──────────────


def test_collapsed_paths_are_self_consistent() -> None:
    """Every node's parent_path must match another node's section_path (or the
    filename root), and no orphan parent_path references survive.
    """
    skeletons = [
        _skel("doc.pdf/A", title="A", level=1, parent_path="doc.pdf"),
        _skel("doc.pdf/A/B", title="B", level=2, parent_path="doc.pdf/A"),
        _skel(
            "doc.pdf/A/B/C1",
            title="C1",
            level=3,
            parent_path="doc.pdf/A/B",
        ),
        _skel(
            "doc.pdf/A/B/C2",
            title="C2",
            level=3,
            parent_path="doc.pdf/A/B",
        ),
    ]

    result = collapse_single_child_chains(skeletons)

    paths = {s.section_path for s in result}
    for skel in result:
        if skel.parent_path is None:
            continue
        # parent_path is either the filename root (not in paths) or a real node.
        assert skel.parent_path == "doc.pdf" or skel.parent_path in paths, (
            f"orphan parent_path {skel.parent_path!r} for {skel.section_path!r}"
        )

    # Titles also must be unique within their parent (path uniqueness).
    assert len(paths) == len(result), "duplicate section_path in result"


# ── Test 7: idempotency — collapsing an already-collapsed list is a no-op


def test_collapse_is_idempotent() -> None:
    """Running collapse twice yields the same list (no further merges)."""
    skeletons = [
        _skel("doc.pdf/A", title="A", level=1, parent_path="doc.pdf"),
        _skel("doc.pdf/A/B", title="B", level=2, parent_path="doc.pdf/A"),
        _skel(
            "doc.pdf/A/B/C",
            title="C",
            level=3,
            parent_path="doc.pdf/A/B",
        ),
    ]

    once = collapse_single_child_chains(skeletons)
    twice = collapse_single_child_chains(once)

    assert [s.to_dict() for s in once] == [s.to_dict() for s in twice]


# ── Test 8: degenerate inputs ────────────────────────────────────────


def test_empty_list_returns_empty() -> None:
    assert collapse_single_child_chains([]) == []


def test_single_node_unchanged() -> None:
    only = _skel("doc.pdf/A", title="A", level=1, parent_path="doc.pdf")
    result = collapse_single_child_chains([only])
    assert len(result) == 1
    assert result[0].to_dict() == only.to_dict()


# ── Test 9: multiple top-level chains collapse independently ─────────


def test_multiple_top_level_roots_collapse_independently() -> None:
    """Two sibling top-level sections each with a single child both merge."""
    skeletons = [
        _skel("doc.pdf/A", title="A", level=1, parent_path="doc.pdf"),
        _skel("doc.pdf/A/A1", title="A1", level=2, parent_path="doc.pdf/A"),
        _skel("doc.pdf/B", title="B", level=1, parent_path="doc.pdf"),
        _skel("doc.pdf/B/B1", title="B1", level=2, parent_path="doc.pdf/B"),
    ]

    result = collapse_single_child_chains(skeletons)
    titles = sorted(s.title for s in result)
    assert titles == ["A A1", "B B1"]


# ── Test 10: parent preserves its own page range ─────────────────────


def test_parent_keeps_its_own_page_range() -> None:
    """Merged node keeps the parent's start/end_page, not the child's."""
    skeletons = [
        _skel(
            "doc.pdf/A",
            title="A",
            level=1,
            parent_path="doc.pdf",
            start_page=10,
            end_page=20,
        ),
        _skel(
            "doc.pdf/A/B",
            title="B",
            level=2,
            parent_path="doc.pdf/A",
            start_page=12,
            end_page=18,
        ),
    ]

    result = collapse_single_child_chains(skeletons)

    assert result[0].start_page == 10
    assert result[0].end_page == 20


# ═══════════════════════════════════════════════════════════════════════
# TitleNode-level collapse (operates on the raw TOC tree, before page
# resolution; catches implicit-ancestor single-child chains that the
# skeleton-level pass cannot see because internal nodes are never
# materialized as SectionSkeleton rows).
# ═══════════════════════════════════════════════════════════════════════


def _node(
    title: str,
    level: int,
    *,
    children: list[TitleNode] | None = None,
    printed_page: int | None = None,
) -> TitleNode:
    return TitleNode(
        title=title,
        level=level,
        printed_page=printed_page,
        children=list(children or []),
    )


def _count_nodes(nodes: list[TitleNode]) -> int:
    return sum(1 + _count_nodes(n.children) for n in nodes)


def _render_titles(nodes: list[TitleNode], depth: int = 0) -> list[str]:
    out: list[str] = []
    for n in nodes:
        out.append("  " * depth + f"L{n.level} [{n.title}]")
        out.extend(_render_titles(n.children, depth + 1))
    return out


# ── TN Test 1: 大类 → 代号 → 文档 深链折叠（核心用例）─────────────────


def test_title_node_category_code_doc_chain_collapses() -> None:
    """Mirrors the SJSYJ-SC document: 大类 → 代号 → 多个文档.

    大类只有 1 个子（代号），代号有多个子（文档）。
    折叠后：大类+代号合并为 L1，文档从 L3 提升为 L2。
    """
    forest = [
        _node("安全类", 1, children=[
            _node("SJSYJ-SC103", 2, children=[
                _node("制度一", 3, printed_page=137),
                _node("制度二", 3, printed_page=142),
            ]),
        ]),
    ]

    result = collapse_title_node_single_child_chains(forest)

    assert _count_nodes(result) == 3  # merged L1 + 2 docs (was 4)
    assert result[0].title == "安全类 SJSYJ-SC103"
    assert result[0].level == 1
    assert len(result[0].children) == 2
    # Children re-leveled L3 → L2
    assert {c.level for c in result[0].children} == {2}
    assert {c.title for c in result[0].children} == {"制度一", "制度二"}


def test_title_node_full_linear_chain_collapses_to_leaf() -> None:
    """大类 → 代号 → 单文档（每个都单子）应折叠成单个 L1 叶子."""
    forest = [
        _node("科技成果类", 1, children=[
            _node("SJSYJ-SC104", 2, children=[
                _node("科技创新管理办法", 3, printed_page=379),
            ]),
        ]),
    ]

    result = collapse_title_node_single_child_chains(forest)

    assert len(result) == 1
    assert result[0].title == "科技成果类 SJSYJ-SC104 科技创新管理办法"
    assert result[0].level == 1
    assert result[0].printed_page == 379  # absorbed deepest leaf's page
    assert result[0].children == []


# ── TN Test 2: 多子节点不折叠 ────────────────────────────────────────


def test_title_node_multiple_children_not_collapsed() -> None:
    forest = [
        _node("Root", 1, children=[
            _node("A", 2),
            _node("B", 2),
        ]),
    ]

    result = collapse_title_node_single_child_chains(forest)

    assert len(result) == 1
    assert result[0].title == "Root"  # unchanged
    assert len(result[0].children) == 2


# ── TN Test 3: 兄弟链各自独立折叠 ──────────────────────────────────


def test_title_node_sibling_chains_collapse_independently() -> None:
    forest = [
        _node("经营类", 1, children=[
            _node("SC101", 2, children=[_node("doc1", 3)]),
        ]),
        _node("生产类", 1, children=[
            _node("SC102", 2, children=[_node("doc2", 3), _node("doc3", 3)]),
        ]),
    ]

    result = collapse_title_node_single_child_chains(forest)

    titles = [n.title for n in result]
    # 经营类 chain is fully single-child → collapses all the way to the leaf.
    assert titles == ["经营类 SC101 doc1", "生产类 SC102"]
    # 经营类: fully collapsed → leaf
    assert result[0].children == []
    assert result[0].level == 1
    # 生产类: SC102 has 2 children → 生产类 absorbs SC102, docs promote to L2.
    assert len(result[1].children) == 2
    assert {c.title for c in result[1].children} == {"doc2", "doc3"}
    assert {c.level for c in result[1].children} == {2}


# ── TN Test 4: 等级连续性（无 gap）──────────────────────────────────


def test_title_node_levels_stay_contiguous_after_collapse() -> None:
    """Absorbing a node must re-level the promoted subtree so no L1→L3 gap."""
    forest = [
        _node("A", 1, children=[
            _node("B", 2, children=[
                _node("C", 3, children=[
                    _node("D", 4),
                    _node("E", 4),
                ]),
            ]),
        ]),
    ]

    result = collapse_title_node_single_child_chains(forest)

    # A+B+C chain collapses into "A B C" (L1), D/E promote to L2.
    assert result[0].title == "A B C"
    assert result[0].level == 1
    levels = sorted(c.level for c in result[0].children)
    assert levels == [2, 2]


# ── TN Test 5: page hint 优先取子的 ─────────────────────────────────


def test_title_node_merged_page_hint_prefers_child() -> None:
    """A merged node uses the child's (deeper, more accurate) page anchor."""
    forest = [
        _node("Parent", 1, printed_page=1, children=[
            _node("Child", 2, printed_page=42, children=[
                _node("Leaf", 3),
            ]),
        ]),
    ]

    result = collapse_title_node_single_child_chains(forest)

    # Parent+Child merge first (parent's page 1, child's page 42 → pick 42).
    # Then merged+Leaf merge: merged page 42, leaf has no page → 42.
    assert result[0].printed_page == 42


# ── TN Test 6: 不做标题过滤（"代号型"照样合并）──────────────────────


def test_title_node_code_label_merges_without_filtering() -> None:
    forest = [
        _node("安全管理类", 1, children=[
            _node("SC-001", 2, children=[
                _node("actual content", 3),
            ]),
        ]),
    ]

    result = collapse_title_node_single_child_chains(forest)

    assert result[0].title == "安全管理类 SC-001 actual content"


# ── TN Test 7: 退化输入 ─────────────────────────────────────────────


def test_title_node_empty_forest_returns_empty() -> None:
    assert collapse_title_node_single_child_chains([]) == []


def test_title_node_single_leaf_unchanged() -> None:
    only = _node("Solo", 1)
    result = collapse_title_node_single_child_chains([only])
    assert len(result) == 1
    assert result[0].title == "Solo"
    assert result[0].children == []


# ── TN Test 8: 幂等 ─────────────────────────────────────────────────


def test_title_node_collapse_is_idempotent() -> None:
    forest = [
        _node("A", 1, children=[
            _node("B", 2, children=[
                _node("C", 3),
            ]),
        ]),
    ]
    once = collapse_title_node_single_child_chains(forest)
    twice = collapse_title_node_single_child_chains(once)

    assert _render_titles(once) == _render_titles(twice)
