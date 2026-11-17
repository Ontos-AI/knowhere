"""Text and media follow the returned pages: coarse lit pages, page video/audio."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from typing import Any

import pytest

from shared.services.chunks.asset_descriptor import page_media_marker
from shared.services.chunks.page_texts import PAGE_TEXTS_KEY, narrow_to_pages
from shared.services.retrieval.hydration.evidence_compose import compose_evidence_parts
from shared.services.retrieval.hydration.result_assembly import (
    RetrievalResultRows,
    assemble_retrieval_results,
)
from shared.services.retrieval.scoring.knowhere_provider import (
    KnowhereProvider,
    SectionRow,
    UnitRow,
)


def _video(asset_id: str, page: int, title: str) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "kind": "video",
        "origin": "embedded",
        "page_nums": [page],
        "ref": {"kind": "artifact", "path": f"embedded_media/{asset_id}.mp4"},
        "title": title,
        "duration_ms": 128_000,
    }


VIDEO_2 = _video("vid-2", 2, "Intro")
VIDEO_3 = _video("vid-3", 3, "Course")


def _coarse_metadata() -> dict[str, Any]:
    return {
        "enrichment_state": "coarse",
        "page_nums": [1, 2, 3],
        PAGE_TEXTS_KEY: [
            {"page_num": 1, "text": "page one"},
            {"page_num": 2, "text": "page two"},
            {"page_num": 3, "text": "page three"},
        ],
        "assets": [VIDEO_2, VIDEO_3],
        "connect_to": [
            {"target": "vid-2", "relation": "embeds"},
            {"target": "vid-3", "relation": "embeds"},
        ],
    }


def _media_row(asset: dict[str, Any], transcript: str) -> dict[str, Any]:
    return {
        "chunk_id": asset["asset_id"],
        "chunk_type": "video",
        "content": f"{page_media_marker(asset)}\n\n{transcript}",
        "asset_url": f"https://media.example/{asset['asset_id']}",
        "sort_order": 9,
        "chunk_metadata": {"asset": asset},
    }


def test_narrow_keeps_lit_page_text_with_its_media_marker() -> None:
    metadata = _coarse_metadata()
    content = narrow_to_pages(metadata, [3, 1])
    assert content == f"page one\n\npage three\n{page_media_marker(VIDEO_3)}"
    assert [entry["page_num"] for entry in metadata[PAGE_TEXTS_KEY]] == [1, 3]
    assert [asset["asset_id"] for asset in metadata["assets"]] == ["vid-3"]
    assert [item["target"] for item in metadata["connect_to"]] == ["vid-3"]


def test_narrow_leaves_rows_without_page_text_alone() -> None:
    metadata = {"enrichment_state": "text", "page_nums": [4]}
    assert narrow_to_pages(metadata, [4]) is None
    assert metadata == {"enrichment_state": "text", "page_nums": [4]}


@pytest.mark.asyncio
async def test_coarse_hit_returns_lit_page_text_and_its_video_once() -> None:
    page = {
        "chunk_id": "coarse-1",
        "chunk_type": "page",
        "content": "page one\n\npage two\n\npage three",
        "matched_page_nums": [3],
        "score": 0.7,
        "sort_order": 1,
        "chunk_metadata": _coarse_metadata(),
    }
    video_2 = _media_row(VIDEO_2, "intro words")
    video_3 = _media_row(VIDEO_3, "course words")
    assembled = await assemble_retrieval_results(
        rows=[page, video_3],
        exclude_document_ids=[],
        exclude_sections=[],
        loaded_rows=RetrievalResultRows(
            primary=[page, video_3], connected=[video_2, video_3]
        ),
    )

    assert [row["chunk_id"] for row in assembled] == ["coarse-1", "vid-3"]
    page_row, video_row = assembled
    assert page_row["page_nums"] == [3]
    assert page_row["content"] == f"page three\n{page_media_marker(VIDEO_3)}"
    evidence = "".join(
        part["text"] for part in page_row["composed"] if part["type"] == "text"
    )
    assert evidence == (
        f"page three\n{page_media_marker(VIDEO_3)}\n"
        "https://media.example/vid-3\ncourse words"
    )
    assert "intro words" not in evidence
    assert video_row["score"] == 0.7
    assert video_row["composed"] == []


@pytest.mark.asyncio
async def test_refined_page_shows_owned_video_at_its_marker() -> None:
    marker = page_media_marker(VIDEO_2)
    page = {
        "chunk_id": "leaf-1",
        "chunk_type": "page",
        "content": f"slide two\n{marker}",
        "score": 0.5,
        "sort_order": 1,
        "chunk_metadata": {
            "enrichment_state": "refined",
            "page_nums": [2],
            "assets": [VIDEO_2],
            "connect_to": [{"target": "vid-2", "relation": "embeds"}],
        },
    }
    video = _media_row(VIDEO_2, "intro words")
    assembled = await assemble_retrieval_results(
        rows=[page],
        exclude_document_ids=[],
        exclude_sections=[],
        loaded_rows=RetrievalResultRows(primary=[page], connected=[video]),
    )
    assert [row["chunk_id"] for row in assembled] == ["leaf-1", "vid-2"]
    evidence = "".join(
        part["text"] for part in assembled[0]["composed"] if part["type"] == "text"
    )
    assert evidence == f"slide two\n{marker}\nhttps://media.example/vid-2\nintro words"


def test_page_row_without_page_images_has_no_unavailable_note() -> None:
    parts = compose_evidence_parts(
        {
            "chunk_type": "page",
            "content": "docx page text",
            "chunk_metadata": {"page_nums": [3]},
        },
        {},
    )
    assert parts == [{"type": "text", "text": "docx page text"}]


def _provider_rows() -> tuple[list[SectionRow], list[UnitRow]]:
    sections = [
        SectionRow(
            section_id="sec-root",
            parent_section_id=None,
            section_path="Root",
            section_title="Root",
            section_level=0,
            summary="",
            sort_order=0,
        ),
        SectionRow(
            section_id="sec-p2",
            parent_section_id=None,
            section_path="deck.pptx/p2",
            section_title="p2",
            section_level=1,
            summary="",
            sort_order=1,
        ),
    ]
    units = [
        UnitRow(
            chunk_id="page-2",
            section_id="sec-p2",
            chunk_type="page",
            content=f"slide two\n{page_media_marker(VIDEO_2)}",
            sort_order=1,
            metadata={"connect_to": [{"target": "vid-2", "relation": "embeds"}]},
        ),
        UnitRow(
            chunk_id="vid-2",
            section_id="sec-root",
            chunk_type="video",
            content="intro words",
            sort_order=2,
        ),
    ]
    return sections, units


def _unit_ids(provider: KnowhereProvider) -> dict[str, list[str]]:
    return {
        sid: [unit.chunk_id for unit in units]
        for sid, units in provider._units_by_section.items()
    }


def test_provider_indexes_video_with_its_page() -> None:
    sections, units = _provider_rows()
    provider = KnowhereProvider(doc_id="doc-1", sections=sections, units=units)
    assert _unit_ids(provider) == {"sec-root": [], "sec-p2": ["page-2", "vid-2"]}


@pytest.mark.asyncio
async def test_async_provider_indexes_video_with_its_page() -> None:
    sections, units = _provider_rows()

    async def checkpoint() -> None:
        return None

    provider = await KnowhereProvider.createAsync(
        doc_id="doc-1", sections=sections, units=units, checkpoint=checkpoint
    )
    assert _unit_ids(provider) == {"sec-root": [], "sec-p2": ["page-2", "vid-2"]}


class _Mapped:
    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[_Mapped]:
        return [_Mapped(row) for row in self._rows]


class _Session:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def execute(self, *_args: object, **_kwargs: object) -> _Result:
        return _Result(self._rows)


def _section_row(
    chunk_id: str, chunk_type: str, sort_order: int, section_path: str = "Root"
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "document_id": "doc-1",
        "section_id": f"sec-{section_path}",
        "chunk_type": chunk_type,
        "content": chunk_id,
        "source_chunk_path": None,
        "file_path": None,
        "chunk_metadata": {},
        "job_result_id": "jr-1",
        "sort_order": sort_order,
        "section_path": section_path,
        "source_file_name": "notes.md",
        "job_id": "job-1",
        "result_raw_prefix": None,
    }


async def _hydrate(
    rows: list[dict[str, Any]], section_path: str = "Root"
) -> tuple[list[dict[str, Any]], bool]:
    from shared.services.retrieval.search.map_unit_discovery import (
        _hydrate_winning_units,
    )

    unit = {
        "document_id": "doc-1",
        "job_result_id": "jr-1",
        "section_id": f"sec-{section_path}",
    }
    return await _hydrate_winning_units(
        _Session(rows),  # type: ignore[arg-type]
        user_id="user-1",
        namespace="ns",
        ranked_unit_ids=["unit-1"],
        rows_by_unit_id={"unit-1": unit},
        scores_by_unit={"unit-1": 1.0},
        chunk_types=None,
        exclude_document_ids=[],
        exclude_sections=[],
        revision_pins=None,
    )


@pytest.mark.asyncio
async def test_root_hit_returns_its_own_text_not_parked_assets() -> None:
    primaries, unhydrated = await _hydrate(
        [
            _section_row("table-1", "table", 0),
            _section_row("opening-text", "text", 1),
            _section_row("vid-1", "video", 2),
            _section_row("closing-text", "text", 3),
        ]
    )
    assert [row["chunk_id"] for row in primaries] == ["opening-text", "closing-text"]
    assert [row["score"] for row in primaries] == [1.0, 1.0]
    assert unhydrated is False


@pytest.mark.asyncio
async def test_root_of_parked_assets_alone_returns_nothing() -> None:
    primaries, unhydrated = await _hydrate([_section_row("table-1", "table", 0)])
    assert primaries == []
    assert unhydrated is False


@pytest.mark.asyncio
async def test_section_that_is_a_table_returns_the_table() -> None:
    primaries, _unhydrated = await _hydrate(
        [_section_row("sheet-table", "table", 0, section_path="book.xlsx/Sheet1")],
        section_path="book.xlsx/Sheet1",
    )
    assert [row["chunk_id"] for row in primaries] == ["sheet-table"]


@pytest.mark.asyncio
async def test_section_without_rows_is_unhydrated() -> None:
    primaries, unhydrated = await _hydrate([])
    assert primaries == []
    assert unhydrated is True
