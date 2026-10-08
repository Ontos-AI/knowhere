from __future__ import annotations

import json
import os
import re

import pytest
from pytest import MonkeyPatch

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


def test_refine_large_leaf_preserves_headings_when_provider_output_is_truncated(
    monkeypatch: MonkeyPatch,
) -> None:
    """A bounded provider response must never collapse a whole PDF into Root."""

    class BoundedClient:
        def chat_completion(self, **kwargs: object) -> str:
            messages = kwargs["messages"]
            assert isinstance(messages, list)
            prompt: str = str(messages[-1]["content"])
            identifiers: list[int] = [
                int(value) for value in re.findall(r'"id": (\d+)', prompt)
            ]
            response: str = json.dumps(
                [{"id": identifier, "level": 1} for identifier in identifiers]
            )
            # SpaceX's real response stopped mid-object at its completion budget.
            budget: int = int(str(kwargs["max_tokens"]))
            return response[: budget * 2]

    monkeypatch.setattr(
        fine_hierarchy,
        "get_text_client",
        lambda requested_model=None: (BoundedClient(), "test-model"),
    )
    skeleton: SectionSkeleton = SectionSkeleton(
        section_path="spacex.pdf/Root",
        level=1,
        start_page=1,
        end_page=407,
        title="Root",
        parent_path="spacex.pdf",
    )
    tags: list[PageTagResult] = [
        PageTagResult(
            page_index=page,
            observed_titles=[{"text": f"Section {page}", "prominence": 1.0}],
        )
        for page in range(1, 408)
    ]

    refined: list[SectionSkeleton] = fine_hierarchy.refine_fat_leaf_skeletons(
        coarse_skeletons=[skeleton],
        tag_results=tags,
        fat_leaf_pages=set(range(1, 408)),
        model_name="test-model",
        max_tokens=2000,
    )

    assert len(refined) == 407
    assert [item.start_page for item in refined] == list(range(1, 408))
    assert all(item.end_page == item.start_page for item in refined)


def test_refine_batches_preserve_parent_context_and_recover_incomplete_classification(
    monkeypatch: MonkeyPatch,
) -> None:
    prompts: list[str] = []

    class IncompleteClient:
        def chat_completion(self, **kwargs: object) -> str:
            messages = kwargs["messages"]
            assert isinstance(messages, list)
            prompt: str = str(messages[-1]["content"])
            prompts.append(prompt)
            identifiers: list[int] = [
                int(value) for value in re.findall(r'"id": (\d+)', prompt)
            ]
            # A well-formed but partial array must also trigger smaller requests.
            if len(identifiers) > 2:
                identifiers = identifiers[:-1]
            return json.dumps(
                [
                    {
                        "id": identifier,
                        "level": 1
                        if identifier == 1
                        else (0 if identifier == 4 else 2),
                    }
                    for identifier in identifiers
                ]
            )

    monkeypatch.setattr(
        fine_hierarchy,
        "get_text_client",
        lambda requested_model=None: (IncompleteClient(), "test-model"),
    )
    skeleton: SectionSkeleton = SectionSkeleton(
        section_path="demo.pdf/Root",
        level=1,
        start_page=1,
        end_page=8,
        title="Root",
        parent_path="demo.pdf",
    )
    tags: list[PageTagResult] = [
        PageTagResult(
            page_index=page,
            observed_titles=[{"text": f"Heading {page}", "prominence": 1.0}],
        )
        for page in range(1, 9)
    ]
    refined: list[SectionSkeleton] = fine_hierarchy.refine_fat_leaf_skeletons(
        coarse_skeletons=[skeleton],
        tag_results=tags,
        fat_leaf_pages=set(range(1, 9)),
        max_tokens=128,
        model_name="test-model",
    )

    assert [item.title for item in refined] == [
        f"Heading {page}" for page in [1, 2, 3, 5, 6, 7, 8]
    ]
    assert all(item.parent_path == "demo.pdf/Root/Heading 1" for item in refined[1:])
    assert any(
        "level=1, heading=Heading 1" in prompt and '"id": 5' in prompt
        for prompt in prompts
    )


def test_refine_rejects_exhausted_invalid_output_instead_of_publishing_root(
    monkeypatch: MonkeyPatch,
) -> None:
    class InvalidClient:
        def chat_completion(self, **kwargs: object) -> str:
            return '[{"id": 1, "level":'

    monkeypatch.setattr(
        fine_hierarchy,
        "get_text_client",
        lambda requested_model=None: (InvalidClient(), "test-model"),
    )
    skeleton: SectionSkeleton = SectionSkeleton(
        section_path="demo.pdf/Root",
        level=1,
        start_page=1,
        end_page=407,
        title="Root",
        parent_path="demo.pdf",
    )
    with pytest.raises(ValueError, match="incomplete or malformed"):
        fine_hierarchy.refine_fat_leaf_skeletons(
            coarse_skeletons=[skeleton],
            tag_results=[
                PageTagResult(
                    page_index=1,
                    observed_titles=[
                        {"text": "Launch capabilities", "prominence": 1.0}
                    ],
                )
            ],
            fat_leaf_pages={1},
            model_name="test-model",
        )


def test_refine_preserves_body_pages_before_first_observed_heading(
    monkeypatch: MonkeyPatch,
) -> None:
    from app.services.page_memory.node_assembler import identify_leaf_nodes

    monkeypatch.setattr(
        fine_hierarchy,
        "get_text_client",
        lambda requested_model=None: (
            _FakeClient([{"id": 1, "level": 1}, {"id": 2, "level": 1}]),
            "test-model",
        ),
    )
    skeleton: SectionSkeleton = SectionSkeleton(
        section_path="demo.pdf/Root",
        level=1,
        start_page=1,
        end_page=10,
        title="Root",
        parent_path="demo.pdf",
    )
    refined: list[SectionSkeleton] = fine_hierarchy.refine_fat_leaf_skeletons(
        coarse_skeletons=[skeleton],
        tag_results=[
            PageTagResult(
                page_index=3,
                observed_titles=[{"text": "Launch capabilities", "prominence": 1.0}],
            ),
            PageTagResult(
                page_index=7,
                observed_titles=[{"text": "Financial statements", "prominence": 1.0}],
            ),
        ],
        fat_leaf_pages=set(range(1, 11)),
        model_name="test-model",
    )
    leaves = identify_leaf_nodes(refined)
    owned_pages: set[int] = {
        page
        for leaf in leaves
        for page in (
            leaf.body_pages
            if leaf.body_pages is not None
            else range(leaf.start_page, leaf.end_page + 1)
        )
    }
    assert owned_pages == set(range(1, 11))


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


def test_compute_fat_leaf_pages_excludes_toc_pages_from_span() -> None:
    """Closed range 1-6 with toc=[2,3,4] is only 3 body pages → not fat when min=4."""
    skeletons = [
        SectionSkeleton(
            section_path="demo.pdf/Abbreviations",
            level=1,
            start_page=1,
            end_page=6,
            title="Abbreviations",
            parent_path="demo.pdf",
        ),
    ]
    assert (
        fine_hierarchy.compute_fat_leaf_pages(
            skeletons,
            min_pages=4,
            toc_pages=[2, 3, 4],
        )
        == set()
    )
    assert fine_hierarchy.compute_fat_leaf_pages(
        skeletons,
        min_pages=2,
        toc_pages=[2, 3, 4],
    ) == {1, 5, 6}


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
        "Section A",
        "A.1 Last Heading",
        "Section C",
        "B.1 Boundary Heading",
    ]
    assert refined[1].parent_path == "demo.pdf/Section A"
    assert refined[3].parent_path == "demo.pdf/Section B"


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

    assert [item.title for item in refined] == ["History", "NCC 2022"]
    assert all("amendment" not in item.title.casefold() for item in refined)
    assert all(item.start_page != 23 for item in refined)
