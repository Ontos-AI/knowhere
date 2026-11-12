"""Media two-level outline: chapters, parts, summaries, and doc_nav handoff."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from types import ModuleType

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

_MINUTE_MS = 60_000
_BOGLE_YOUTH = (
    "Jack Bogle grows up during the Depression, works through school, and "
    "writes a Princeton thesis arguing mutual funds should serve shareholders."
)
_FOUNDING = (
    "Bogle is fired from Wellington, wins the fund boards, and founds Vanguard "
    "as a mutually owned company that later launches the first index fund."
)


def _module(name: str) -> ModuleType:
    # Resolve the live module: contract fixtures may reload app modules.
    return importlib.import_module(name)


def _segments(count: int) -> list[object]:
    segments_module = _module("app.services.document_parser.formats.media.segments")
    return [
        segments_module.TranscriptSegment(
            start_ms=index * _MINUTE_MS,
            end_ms=index * _MINUTE_MS + 30_000,
            text=f"S1: line {index}",
            speakers=("S1",),
        )
        for index in range(count)
    ]


def _patch_outline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    titles: list[str] | None,
    parts: list[tuple[int, str]] | None,
) -> list[dict[str, object]]:
    outline = _module("app.services.document_parser.formats.media.outline")
    summary_calls: list[dict[str, object]] = []

    def _fake_summary(**kwargs: object) -> str:
        summary_calls.append(dict(kwargs))
        return f"summary of {kwargs['node_name']}"

    monkeypatch.setattr(outline, "chapter_titles", lambda _segments: titles)
    monkeypatch.setattr(outline, "part_boundaries", lambda _digests: parts)
    monkeypatch.setattr(outline, "llm_summarize_scope", _fake_summary)
    return summary_calls


def test_no_chapters_keeps_flat_paths_and_no_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_outline(monkeypatch, titles=None, parts=None)
    outline = _module("app.services.document_parser.formats.media.outline")
    result = outline.build_media_outline("talk.mp3", _segments(3))
    assert result.paths == ["talk.mp3"] * 3
    assert result.section_summaries == {}
    assert calls == []


def test_chapters_without_parts_sit_under_root_with_chapter_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_outline(
        monkeypatch, titles=["Intro", "Intro", "Pricing", "Intro"], parts=None
    )
    outline = _module("app.services.document_parser.formats.media.outline")
    result = outline.build_media_outline("talk.mp3", _segments(4))
    assert result.paths == [
        "talk.mp3/Intro",
        "talk.mp3/Intro",
        "talk.mp3/Pricing",
        "talk.mp3/Intro_2",
    ]
    assert result.section_summaries == {
        "talk.mp3/Intro": "summary of Intro",
        "talk.mp3/Pricing": "summary of Pricing",
        "talk.mp3/Intro_2": "summary of Intro_2",
    }
    first = calls[0]
    assert first["self_only"] == "S1: line 0\nS1: line 1"
    assert first["child_rows"] == []
    assert first["usage_task"] == outline.CHAPTER_SUMMARY_USAGE_TASK
    assert first["max_tokens"] == _module(
        "app.services.connect_builder.summary_builder"
    ).SUMMARY_MAX_LEN


def test_parts_group_chapters_and_summarize_from_chapter_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_outline(
        monkeypatch,
        titles=["Early life", "Princeton", "Wellington", "Index fund"],
        parts=[(0, "Bogle's youth"), (2, "Building Vanguard")],
    )
    outline = _module("app.services.document_parser.formats.media.outline")
    result = outline.build_media_outline("vanguard.mp3", _segments(4))
    assert result.paths == [
        "vanguard.mp3/Bogle's youth/Early life",
        "vanguard.mp3/Bogle's youth/Princeton",
        "vanguard.mp3/Building Vanguard/Wellington",
        "vanguard.mp3/Building Vanguard/Index fund",
    ]
    assert result.section_summaries["vanguard.mp3/Bogle's youth"] == (
        "summary of Bogle's youth"
    )
    assert result.section_summaries["vanguard.mp3/Building Vanguard/Index fund"] == (
        "summary of Index fund"
    )
    part_call = next(call for call in calls if call["node_name"] == "Building Vanguard")
    assert part_call["self_only"] == ""
    assert part_call["child_rows"] == [
        ("Wellington", "summary of Wellington"),
        ("Index fund", "summary of Index fund"),
    ]
    assert part_call["usage_task"] == outline.PART_SUMMARY_USAGE_TASK


def test_empty_llm_summaries_are_left_to_doc_nav_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outline = _module("app.services.document_parser.formats.media.outline")
    monkeypatch.setattr(outline, "chapter_titles", lambda _segments: ["A", "B"])
    monkeypatch.setattr(outline, "part_boundaries", lambda _digests: None)
    monkeypatch.setattr(outline, "llm_summarize_scope", lambda **_kwargs: "")
    result = outline.build_media_outline("talk.mp3", _segments(2))
    assert result.paths == ["talk.mp3/A", "talk.mp3/B"]
    assert result.section_summaries == {}


class _FakeClient:
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.prompt = ""
        self.usage_task = ""

    def chat_completion(self, *, messages: list[dict[str, str]], usage_task: str) -> str:
        self.prompt = messages[1]["content"]
        self.usage_task = usage_task
        return json.dumps(self.reply)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            [{"start": 0, "title": "Youth"}, {"start": 2, "title": "Vanguard"}],
            [(0, "Youth"), (2, "Vanguard")],
        ),
        ([{"start": 0, "title": "Everything"}], None),
        (
            [
                {"start": 0, "title": "One"},
                {"start": 1, "title": "Two"},
                {"start": 2, "title": "Three"},
            ],
            None,
        ),
        ([{"start": 1, "title": "Skips first"}, {"start": 2, "title": "B"}], None),
    ],
)
def test_part_boundaries_need_two_valid_parts(
    monkeypatch: pytest.MonkeyPatch,
    reply: object,
    expected: object,
) -> None:
    chapters = _module("app.services.document_parser.formats.media.chapters")
    client = _FakeClient(reply)
    monkeypatch.setitem(
        chapters.part_boundaries.__globals__,
        "get_text_client",
        lambda: (client, "model"),
    )
    digests = [
        chapters.ChapterDigest(
            title=f"Chapter {index}",
            start_ms=index * _MINUTE_MS,
            end_ms=(index + 1) * _MINUTE_MS,
            summary=f"About {index}",
        )
        for index in range(3)
    ]
    assert chapters.part_boundaries(digests) == expected
    assert client.usage_task == chapters.PARTS_USAGE_TASK
    assert "[1] 00:01:00-00:02:00 Chapter 1\nAbout 1" in client.prompt


def test_doc_nav_takes_parser_summaries_and_only_adds_top_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    summary_builder = _module("app.services.connect_builder.summary_builder")
    workspace = tmp_path / "workspace"
    file_dir = workspace / "vanguard.mp3"
    file_dir.mkdir(parents=True)
    chunks = [
        {"type": "text", "path": "vanguard.mp3/Youth/Early life", "content": "S1: born"},
        {"type": "text", "path": "vanguard.mp3/Youth/Princeton", "content": "S1: thesis"},
        {"type": "text", "path": "vanguard.mp3/Vanguard/Index fund", "content": "S2: index"},
    ]
    (file_dir / summary_builder.PARSER_SECTION_SUMMARIES_REL).write_text(
        json.dumps(
            {
                "vanguard.mp3/Youth": _BOGLE_YOUTH,
                "vanguard.mp3/Youth/Early life": "Childhood.",
                "vanguard.mp3/Youth/Princeton": "Thesis on funds.",
                "vanguard.mp3/Vanguard": _FOUNDING,
                "vanguard.mp3/Vanguard/Index fund": "First index fund.",
            }
        ),
        encoding="utf-8",
    )
    top_calls: list[dict[str, object]] = []

    def _fake_llm(**kwargs: object) -> str:
        top_calls.append(dict(kwargs))
        return "Vanguard's story."

    monkeypatch.setattr(summary_builder, "llm_summarize_scope", _fake_llm)
    summary_builder.ensure_doc_nav_json(str(file_dir), chunks, source_file_name="vanguard.mp3")
    summary_builder.enrich_doc_nav_summaries(
        str(workspace),
        source_file="vanguard.mp3",
        use_llm=False,
        top_summary_use_llm=True,
        chunks=chunks,
    )
    doc_nav = json.loads((file_dir / "doc_nav.json").read_text(encoding="utf-8"))
    youth = doc_nav["sections"][0]
    assert youth["summary"] == _BOGLE_YOUTH
    assert youth["children"][1]["summary"] == "Thesis on funds."
    assert doc_nav["sections"][1]["summary"] == _FOUNDING
    assert doc_nav["top_summary"] == "Vanguard's story."
    assert len(top_calls) == 1
    assert top_calls[0]["node_name"] == "Document Overview"
