"""TOC ownership nodes claim directory pages under the covering parent."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.structure.anchoring_primitives import (
    SkeletonAnchor,
    serialize_skeleton_anchor,
    serialize_title_node,
)
from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    TitleNode,
)
from app.services.page_memory._serialization import build_hierarchy_tree
from app.services.page_memory._utils import is_toc_node
from app.services.page_memory.fine_hierarchy import (
    compute_fat_leaf_pages,
    refine_fat_leaf_skeletons,
)
from app.services.page_memory.node_assembler import (
    assign_pages_to_leaves,
    identify_leaf_nodes,
)
from app.services.page_memory.phase_a import build_phase_a
from app.services.page_memory.skeleton_extractor import (
    SectionSkeleton,
    collapse_single_child_chains,
    extract_section_skeletons,
)


def _anchor(overrides: dict[tuple[str, ...], int]) -> SkeletonAnchor:
    return SkeletonAnchor(
        offset=0,
        offset_status="ok",
        match_overrides={
            path: TitleMatch(
                page=page,
                source="anchored",
                matched_line=path[-1],
                candidates=[page],
                evidence={},
            )
            for path, page in overrides.items()
        },
        null_page_report=[],
        bulk_count=len(overrides),
        pruned_count=0,
        locate_method="offset_guided_bulk",
    )


def _anatomy(
    *,
    page_count: int,
    nodes: list[TitleNode],
    overrides: dict[tuple[str, ...], int],
    hierarchies: list[dict[str, object]],
    toc_pages: list[int],
    pending_skeleton_anchors: list[dict[str, object]] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        page_count=page_count,
        toc_result=SimpleNamespace(toc_pages=toc_pages),
        toc_hierarchies=hierarchies,
        skeleton_anchor=serialize_skeleton_anchor(_anchor(overrides)),
        skeleton_nodes=[serialize_title_node(node) for node in nodes],
        pending_skeleton_anchors=list(pending_skeleton_anchors or []),
    )


def _extract(
    *,
    page_count: int,
    nodes: list[TitleNode],
    overrides: dict[tuple[str, ...], int],
    hierarchies: list[dict[str, object]],
    toc_pages: list[int],
    filename: str = "doc.pdf",
    pending_skeleton_anchors: list[dict[str, object]] | None = None,
) -> list[SectionSkeleton]:
    return extract_section_skeletons(
        anatomy=_anatomy(
            page_count=page_count,
            nodes=nodes,
            overrides=overrides,
            hierarchies=hierarchies,
            toc_pages=toc_pages,
            pending_skeleton_anchors=pending_skeleton_anchors,
        ),
        filename=filename,
        page_texts={page: "body" for page in range(1, page_count + 1)},
    )


def test_front_toc_is_root_sibling_and_owns_only_toc_pages() -> None:
    skeletons = _extract(
        page_count=20,
        nodes=[TitleNode(title="Chapter 1", level=1, printed_page=16)],
        overrides={("Chapter 1",): 16},
        hierarchies=[
            {
                "toc_range": [14, 15],
                "toc_range_unit": "page",
                "toc_with_level": [
                    {"heading": "Chapter 1", "level": 1, "page_number": 16}
                ],
            }
        ],
        toc_pages=[14, 15],
    )
    by_title = {item.title: item for item in skeletons}

    assert by_title["Root"].evidence["source"] == "document_prefix"
    assert by_title["Root"].start_page == 1
    assert by_title["Root"].end_page == 13
    assert by_title["TOC"].section_path == "doc.pdf/TOC"
    assert by_title["TOC"].parent_path == "doc.pdf"
    assert by_title["TOC"].start_page == 14
    assert by_title["TOC"].end_page == 15
    assert is_toc_node(by_title["TOC"])
    assert by_title["Chapter 1"].start_page == 16
    assert "TOC" in build_hierarchy_tree(skeletons)
    assert "Root" not in build_hierarchy_tree(skeletons)

    phase_a = build_phase_a(
        profile=SimpleNamespace(
            page_count=20,
            anatomy=_anatomy(
                page_count=20,
                nodes=[TitleNode(title="Chapter 1", level=1, printed_page=16)],
                overrides={("Chapter 1",): 16},
                hierarchies=[
                    {
                        "toc_range": [14, 15],
                        "toc_range_unit": "page",
                        "toc_with_level": [
                            {"heading": "Chapter 1", "level": 1, "page_number": 16}
                        ],
                    }
                ],
                toc_pages=[14, 15],
            ),
            page_full_text_cache={
                page: f"text {page}" for page in range(1, 21)
            },
            page_body_text_cache={
                page: f"body {page}" for page in range(1, 21)
            },
        ),
        filename="doc.pdf",
    )
    rows = {row["path"]: row for _, row in phase_a.coarse_df.iterrows()}
    assert rows["doc.pdf"]["page_nums"] == "1,2,3,4,5,6,7,8,9,10,11,12,13"
    assert rows["doc.pdf/TOC"]["page_nums"] == "14,15"
    assert rows["doc.pdf/Chapter 1"]["page_nums"].startswith("16")
    toc_scopes = [
        scope
        for scope in phase_a.manifest["scopes"]
        if all(is_toc_node(SimpleNamespace(evidence=item["evidence"])) for item in scope["skeletons"])
    ]
    assert toc_scopes
    assert toc_scopes[0]["processing_pages"] == []


def test_contained_toc_mounts_under_covering_chapter() -> None:
    chapter = TitleNode(
        title="Chapter 5",
        level=1,
        printed_page=40,
        children=[TitleNode(title="5.1", level=2, printed_page=60)],
    )
    skeletons = _extract(
        page_count=80,
        nodes=[chapter],
        overrides={("Chapter 5",): 40, ("Chapter 5", "5.1"): 60},
        hierarchies=[
            {
                "toc_range": [1, 2],
                "toc_range_unit": "page",
                "toc_with_level": [
                    {"heading": "Chapter 5", "level": 1, "page_number": 40},
                    {"heading": "5.1", "level": 2, "page_number": 60},
                ],
            },
            {
                "toc_range": [50, 51],
                "toc_range_unit": "page",
                "toc_tree": {},
            },
        ],
        toc_pages=[1, 2, 50, 51],
    )
    toc_nodes = [item for item in skeletons if is_toc_node(item)]
    nested = [item for item in toc_nodes if item.parent_path == "doc.pdf/Chapter 5"]
    root_toc = [item for item in toc_nodes if item.parent_path == "doc.pdf"]

    assert len(root_toc) == 1
    assert root_toc[0].section_path == "doc.pdf/TOC"
    assert {root_toc[0].start_page, root_toc[0].end_page} == {1, 2}
    assert len(nested) == 1
    assert nested[0].section_path == "doc.pdf/Chapter 5/TOC"
    assert nested[0].start_page == 50
    assert nested[0].end_page == 51
    tree = build_hierarchy_tree(skeletons)
    assert "TOC" in tree["Chapter 5"]


def test_parallel_root_tocs_merge_page_union() -> None:
    skeletons = _extract(
        page_count=30,
        nodes=[TitleNode(title="Ch1", level=1, printed_page=16)],
        overrides={("Ch1",): 16},
        hierarchies=[
            {
                "toc_range": [10, 11],
                "toc_range_unit": "page",
                "toc_with_level": [{"heading": "Ch1", "level": 1, "page_number": 16}],
            },
            {
                "toc_range": [14, 15],
                "toc_range_unit": "page",
                "toc_tree": {},
            },
        ],
        toc_pages=[10, 11, 14, 15],
    )
    toc_nodes = [item for item in skeletons if is_toc_node(item)]
    assert len(toc_nodes) == 1
    assert toc_nodes[0].parent_path == "doc.pdf"
    assert toc_nodes[0].start_page == 10
    assert toc_nodes[0].end_page == 15

    phase_a = build_phase_a(
        profile=SimpleNamespace(
            page_count=30,
            anatomy=_anatomy(
                page_count=30,
                nodes=[TitleNode(title="Ch1", level=1, printed_page=16)],
                overrides={("Ch1",): 16},
                hierarchies=[
                    {
                        "toc_range": [10, 11],
                        "toc_range_unit": "page",
                        "toc_with_level": [
                            {"heading": "Ch1", "level": 1, "page_number": 16}
                        ],
                    },
                    {
                        "toc_range": [14, 15],
                        "toc_range_unit": "page",
                        "toc_tree": {},
                    },
                ],
                toc_pages=[10, 11, 14, 15],
            ),
            page_full_text_cache={
                page: f"text {page}" for page in range(1, 31)
            },
            page_body_text_cache={
                page: f"body {page}" for page in range(1, 31)
            },
        ),
        filename="doc.pdf",
    )
    toc_row = next(
        row for _, row in phase_a.coarse_df.iterrows() if row["path"] == "doc.pdf/TOC"
    )
    assert toc_row["page_nums"] == "10,11,14,15"


def test_toc_pages_are_skipped_by_fine_and_chapter_processing() -> None:
    chapter = SectionSkeleton(
        "doc.pdf/Chapter 5",
        1,
        40,
        80,
        "Chapter 5",
        "doc.pdf",
    )
    toc = SectionSkeleton(
        "doc.pdf/Chapter 5/TOC",
        2,
        50,
        51,
        "TOC",
        "doc.pdf/Chapter 5",
        evidence={"source": "toc_node"},
    )
    fat_pages = compute_fat_leaf_pages(
        [chapter, toc],
        min_pages=4,
        toc_pages=[50, 51],
    )
    assert 50 not in fat_pages
    assert 51 not in fat_pages
    assert 40 in fat_pages
    assert 80 in fat_pages

    refined = refine_fat_leaf_skeletons(
        coarse_skeletons=[chapter, toc],
        tag_results=[],
        fat_leaf_pages=set(),
    )
    assert [item.title for item in refined] == ["Chapter 5", "TOC"]
    assert is_toc_node(refined[1])


def test_chapter_keeps_pages_after_nested_toc() -> None:
    chapter = SectionSkeleton(
        "doc.pdf/Chapter 5",
        1,
        40,
        80,
        "Chapter 5",
        "doc.pdf",
    )
    toc = SectionSkeleton(
        "doc.pdf/Chapter 5/TOC",
        2,
        50,
        51,
        "TOC",
        "doc.pdf/Chapter 5",
        evidence={"source": "toc_node"},
    )
    leaves = identify_leaf_nodes([chapter, toc])
    views, page_owner = assign_pages_to_leaves(
        leaves,
        available_pages=set(range(40, 81)),
        toc_pages=[50, 51],
    )
    by_title = {view.leaf.title: view for view in views}
    assert 49 in by_title["Chapter 5"].pages
    assert 52 in by_title["Chapter 5"].pages
    assert 80 in by_title["Chapter 5"].pages
    assert 50 not in by_title["Chapter 5"].pages
    assert 51 not in by_title["Chapter 5"].pages
    assert by_title["TOC"].pages == [50, 51]
    assert page_owner[50].title == "TOC"
    assert page_owner[52].title == "Chapter 5"


def test_only_child_toc_does_not_collapse_into_parent_title() -> None:
    parent = SectionSkeleton(
        "doc.pdf/Chapter 5",
        1,
        40,
        80,
        "Chapter 5",
        "doc.pdf",
    )
    toc = SectionSkeleton(
        "doc.pdf/Chapter 5/TOC",
        2,
        50,
        51,
        "TOC",
        "doc.pdf/Chapter 5",
        evidence={"source": "toc_node"},
    )
    result = collapse_single_child_chains([parent, toc])
    titles = [item.title for item in result]
    assert titles == ["Chapter 5", "TOC"]
    assert not any("Chapter 5 TOC" == item.title for item in result)


def test_prefix_stops_before_front_toc_and_every_page_has_an_owner() -> None:
    skeletons = _extract(
        page_count=18,
        nodes=[TitleNode(title="Chapter 1", level=1, printed_page=16)],
        overrides={("Chapter 1",): 16},
        hierarchies=[
            {
                "toc_range": [14, 15],
                "toc_range_unit": "page",
                "toc_with_level": [
                    {"heading": "Chapter 1", "level": 1, "page_number": 16}
                ],
            }
        ],
        toc_pages=[14, 15],
    )
    prefix = next(item for item in skeletons if item.evidence.get("source") == "document_prefix")
    toc = next(item for item in skeletons if is_toc_node(item))
    chapter = next(item for item in skeletons if item.title == "Chapter 1")
    owned: set[int] = set()
    owned.update(range(prefix.start_page, prefix.end_page + 1))
    owned.update(range(toc.start_page, toc.end_page + 1))
    owned.update(range(chapter.start_page, chapter.end_page + 1))
    assert prefix.end_page == 13
    assert 14 not in set(range(prefix.start_page, prefix.end_page + 1))
    assert 15 not in set(range(prefix.start_page, prefix.end_page + 1))
    assert owned == set(range(1, 19))


def test_paired_toc_does_not_cut_primary_window() -> None:
    pending_toc = {
        "toc_range": [7, 12],
        "toc_range_unit": "page",
        "paired_with_range": [1, 6],
        "toc_with_level": [
            {"heading": "General provisions", "level": 1, "page_number": 13}
        ],
    }
    skeletons = _extract(
        page_count=30,
        nodes=[TitleNode(title="总则", level=1, printed_page=13)],
        overrides={("总则",): 13},
        hierarchies=[
            {
                "toc_range": [1, 6],
                "toc_range_unit": "page",
                "toc_with_level": [{"heading": "总则", "level": 1, "page_number": 13}],
            },
            pending_toc,
        ],
        toc_pages=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
        pending_skeleton_anchors=[
            {
                "toc": pending_toc,
                "relationship": "paired",
                "host": "root",
                "paired_with_range": [1, 6],
            }
        ],
    )
    chapter = next(item for item in skeletons if item.title == "总则")
    assert chapter.end_page == 30
    assert all(item.title != "General provisions" for item in skeletons)


def test_paired_toc_pages_merge_into_host_toc_node() -> None:
    pending_toc = {
        "toc_range": [7, 12],
        "toc_range_unit": "page",
        "paired_with_range": [1, 6],
        "toc_tree": {},
    }
    skeletons = _extract(
        page_count=30,
        nodes=[TitleNode(title="总则", level=1, printed_page=13)],
        overrides={("总则",): 13},
        hierarchies=[
            {
                "toc_range": [1, 6],
                "toc_range_unit": "page",
                "toc_with_level": [{"heading": "总则", "level": 1, "page_number": 13}],
            },
            pending_toc,
        ],
        toc_pages=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
        pending_skeleton_anchors=[
            {
                "toc": pending_toc,
                "relationship": "paired",
                "host": "root",
                "paired_with_range": [1, 6],
            }
        ],
    )
    toc_nodes = [item for item in skeletons if is_toc_node(item)]
    assert len(toc_nodes) == 1
    assert toc_nodes[0].section_path == "doc.pdf/TOC"
    assert toc_nodes[0].parent_path == "doc.pdf"
    assert toc_nodes[0].start_page == 1
    assert toc_nodes[0].end_page == 12
