from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.page_memory import fine_hierarchy
from app.services.page_memory.page_tagger import PageTagResult
from app.services.page_memory.skeleton_extractor import SectionSkeleton


class _FakeClient:
    def __init__(self, response: list[dict[str, int]]) -> None:
        self.response = response

    def chat_completion(self, **_kwargs) -> str:
        return json.dumps(self.response)


def test_compute_fat_leaf_pages_uses_exclusive_boundaries() -> None:
    skeletons = [
        SectionSkeleton(
            section_path="demo.pdf/A",
            level=1,
            start_page=10,
            end_page=12,
            title="A",
            parent_path="demo.pdf",
        ),
        SectionSkeleton(
            section_path="demo.pdf/B",
            level=1,
            start_page=12,
            end_page=15,
            title="B",
            parent_path="demo.pdf",
        ),
    ]

    assert fine_hierarchy.compute_fat_leaf_pages(skeletons, min_pages=1) == {
        10,
        11,
        12,
        13,
        14,
        15,
    }


def test_refine_fat_leaf_skeletons_excludes_next_section_start_when_unordered(
    monkeypatch,
) -> None:
    previous = SectionSkeleton(
        section_path="demo.pdf/Section A",
        level=3,
        start_page=225,
        end_page=302,
        title="Section A",
        parent_path="demo.pdf",
    )
    later = SectionSkeleton(
        section_path="demo.pdf/Section C",
        level=3,
        start_page=320,
        end_page=330,
        title="Section C",
        parent_path="demo.pdf",
    )
    next_section = SectionSkeleton(
        section_path="demo.pdf/Section B",
        level=3,
        start_page=302,
        end_page=319,
        title="Section B",
        parent_path="demo.pdf",
    )
    tags = [
        PageTagResult(
            page_index=301,
            observed_titles=[{"text": "A.1 Last Heading", "prominence": 1.0}],
        ),
        PageTagResult(
            page_index=302,
            observed_titles=[
                {
                    "text": "B.1 Boundary Heading",
                    "prominence": 1.0,
                }
            ],
        ),
    ]

    monkeypatch.setattr(
        fine_hierarchy,
        "get_text_client",
        lambda requested_model=None: (
            _FakeClient([{"id": 1, "level": 1}]),
            requested_model,
        ),
    )

    refined = fine_hierarchy.refine_fat_leaf_skeletons(
        coarse_skeletons=[previous, later, next_section],
        tag_results=tags,
        fat_leaf_pages={301, 302},
        model_name="test-model",
    )

    assert [item.title for item in refined] == [
        "A.1 Last Heading",
        "Section C",
        "B.1 Boundary Heading",
    ]
    assert refined[0].parent_path == "demo.pdf/Section A"
    assert refined[2].parent_path == "demo.pdf/Section B"


def test_refine_fat_leaf_skeletons_uses_page_memory_prompt_without_demoting_siblings(
    monkeypatch,
) -> None:
    skeleton = SectionSkeleton(
        section_path="demo.pdf/安全风险分级管控",
        level=1,
        start_page=225,
        end_page=245,
        title="安全风险分级管控",
        parent_path="demo.pdf",
    )
    tags = [
        PageTagResult(
            page_index=225,
            observed_titles=[
                {"text": "安全风险分级管控", "prominence": 1.0},
                {"text": "1 总则", "prominence": 1.0},
            ],
        ),
        PageTagResult(
            page_index=226,
            observed_titles=[
                {"text": "2 术语", "prominence": 1.0},
                {"text": "2.1 定义", "prominence": 0.8},
            ],
        ),
        PageTagResult(
            page_index=228,
            observed_titles=[{"text": "3 基本规定", "prominence": 1.0}],
        ),
    ]

    monkeypatch.setattr(
        fine_hierarchy,
        "get_text_client",
        lambda requested_model=None: (
            _FakeClient(
                [
                    {"id": 1, "level": 1},
                    {"id": 2, "level": 1},
                    {"id": 3, "level": 2},
                    {"id": 4, "level": 1},
                ]
            ),
            requested_model,
        ),
    )

    refined = fine_hierarchy.refine_fat_leaf_skeletons(
        coarse_skeletons=[skeleton],
        tag_results=tags,
        fat_leaf_pages={225, 226, 227, 228},
        model_name="test-model",
    )

    assert [item.title for item in refined] == [
        "1 总则",
        "2 术语",
        "2.1 定义",
        "3 基本规定",
    ]
    assert [item.level for item in refined] == [2, 2, 3, 2]
    assert refined[0].end_page == 225
    assert refined[1].end_page == 227
    assert refined[2].parent_path.endswith("/2 术语")
    assert all(item.title != skeleton.title for item in refined)


def test_refine_fat_leaf_keeps_slash_in_title_as_single_path_segment(
    monkeypatch,
) -> None:
    skeleton = SectionSkeleton(
        section_path="manual.pdf/Index",
        level=1,
        start_page=10,
        end_page=14,
        title="Index",
        parent_path="manual.pdf",
    )
    tags = [
        PageTagResult(
            page_index=10,
            observed_titles=[
                {"text": "Index", "prominence": 1.0},
                {"text": "Symbols/Numbers", "prominence": 1.0},
            ],
        ),
        PageTagResult(
            page_index=12,
            observed_titles=[{"text": "A entries", "prominence": 1.0}],
        ),
    ]

    monkeypatch.setattr(
        fine_hierarchy,
        "get_text_client",
        lambda requested_model=None: (
            _FakeClient(
                [
                    {"id": 1, "level": 1},
                    {"id": 2, "level": 1},
                ]
            ),
            requested_model,
        ),
    )

    refined = fine_hierarchy.refine_fat_leaf_skeletons(
        coarse_skeletons=[skeleton],
        tag_results=tags,
        fat_leaf_pages={10, 11, 12, 13, 14},
        model_name="test-model",
    )

    assert [item.title for item in refined] == ["Symbols/Numbers", "A entries"]
    assert refined[0].section_path == "manual.pdf/Index/Symbols\u2215Numbers"
    assert refined[1].parent_path == "manual.pdf/Index"
    assert refined[0].section_path.count("/") == 2


def test_build_next_title_by_path_preserves_parent_before_same_page_child() -> None:
    parent = SectionSkeleton(
        section_path="demo.pdf/Section Z Parent",
        level=1,
        start_page=10,
        end_page=12,
        title="Section Z Parent",
        parent_path="demo.pdf",
        evidence={"skeleton_kind": "parent_self_only"},
    )
    # Alphabetically sorts before the parent path; stable start_page order
    # must still keep emit order (parent first).
    child = SectionSkeleton(
        section_path="demo.pdf/Section Z Parent/A First Child",
        level=2,
        start_page=10,
        end_page=12,
        title="A First Child",
        parent_path="demo.pdf/Section Z Parent",
    )
    sibling = SectionSkeleton(
        section_path="demo.pdf/Later",
        level=1,
        start_page=13,
        end_page=15,
        title="Later",
        parent_path="demo.pdf",
    )

    next_map = fine_hierarchy.build_next_title_by_path([parent, child, sibling])

    assert next_map[parent.section_path] == "A First Child"
    assert next_map[child.section_path] == "Later"
    assert next_map[sibling.section_path] is None


def test_trim_drops_boundary_page_when_end_anchor_missing() -> None:
    raw = [
        {"heading": "Keep", "page": 10, "key": "keep"},
        {"heading": "Boundary Leftover", "page": 12, "key": "boundaryleftover"},
        {"heading": "Also Boundary", "page": 12, "key": "alsoboundary"},
    ]

    trimmed = fine_hierarchy._trim_by_coarse_anchors(
        raw,
        start_title="",
        end_title="Next Section Title",
        section_path="demo.pdf/Current",
        boundary_page=12,
    )

    assert [item["heading"] for item in trimmed] == ["Keep"]


def test_refine_fat_leaf_drops_boundary_page_on_tail_miss(monkeypatch) -> None:
    previous = SectionSkeleton(
        section_path="demo.pdf/History",
        level=2,
        start_page=14,
        end_page=23,
        title="History",
        parent_path="demo.pdf",
    )
    next_section = SectionSkeleton(
        section_path="demo.pdf/List of amendments",
        level=2,
        start_page=23,
        end_page=30,
        title="List of amendments",
        parent_path="demo.pdf",
    )
    tags = [
        PageTagResult(
            page_index=22,
            observed_titles=[{"text": "NCC 2022", "prominence": 1.0}],
        ),
        PageTagResult(
            page_index=23,
            observed_titles=[
                {"text": "Not The Next Title", "prominence": 1.0},
                {"text": "Another Boundary Title", "prominence": 0.9},
            ],
        ),
    ]

    monkeypatch.setattr(
        fine_hierarchy,
        "get_text_client",
        lambda requested_model=None: (
            _FakeClient([{"id": 1, "level": 1}]),
            requested_model,
        ),
    )

    refined = fine_hierarchy.refine_fat_leaf_skeletons(
        coarse_skeletons=[previous],
        tag_results=tags,
        fat_leaf_pages={22, 23},
        next_title_by_path={previous.section_path: next_section.title},
        model_name="test-model",
    )

    assert [item.title for item in refined] == ["NCC 2022"]
    assert all("amendment" not in item.title.casefold() for item in refined)
    assert all(item.start_page != 23 for item in refined)
