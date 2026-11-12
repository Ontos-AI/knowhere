from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

_MINUTE_MS = 60_000


class _FakeClient:
    def __init__(self, replies: list[object]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []
        self.usage_tasks: list[str] = []

    def chat_completion(self, *, messages: list[dict[str, str]], usage_task: str) -> str:
        self.prompts.append(messages[1]["content"])
        self.usage_tasks.append(usage_task)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)


def _segments(starts_minutes: list[int]) -> list[object]:
    from app.services.document_parser.formats.media.segments import TranscriptSegment

    return [
        TranscriptSegment(
            start_ms=minute * _MINUTE_MS,
            end_ms=minute * _MINUTE_MS + 30_000,
            text=f"S1: segment at {minute}",
            speakers=("S1",),
        )
        for minute in starts_minutes
    ]


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> object:
    from app.services.document_parser.formats.media import chapters

    # Patch through the function globals: contract fixtures may reload modules.
    monkeypatch.setitem(
        chapters.chapter_titles.__globals__,
        "get_text_client",
        lambda: (client, "model"),
    )
    return chapters


def test_one_window_maps_boundaries_to_per_segment_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        [[{"start": 0, "title": "Intro"}, {"start": 2, "title": " Pricing "}]]
    )
    chapters = _patch_client(monkeypatch, client)
    titles = chapters.chapter_titles(_segments([0, 5, 10, 59]))
    assert titles == ["Intro", "Intro", "Pricing", "Pricing"]
    assert client.usage_tasks == [chapters.CHAPTER_USAGE_TASK]
    assert "[0] 00:00:00-00:00:30\nS1: segment at 0" in client.prompts[0]
    assert "[3] 00:59:00-00:59:30" in client.prompts[0]


def test_long_media_is_chaptered_in_sixty_minute_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        [
            [{"start": 0, "title": "Part A"}],
            [{"start": 2, "title": "Part B"}, {"start": 3, "title": "Part C"}],
        ]
    )
    chapters = _patch_client(monkeypatch, client)
    titles = chapters.chapter_titles(_segments([0, 30, 60, 90]))
    assert titles == ["Part A", "Part A", "Part B", "Part C"]
    assert len(client.prompts) == 2
    assert "[0]" in client.prompts[0] and "[2]" not in client.prompts[0]
    assert client.prompts[1].startswith("[2] 01:00:00")


@pytest.mark.parametrize(
    "reply",
    [
        [],
        {"start": 0, "title": "Not a list"},
        [{"start": 1, "title": "Skips the first segment"}],
        [{"start": 0, "title": "A"}, {"start": 0, "title": "B"}],
        [{"start": 0, "title": "A"}, {"start": 9, "title": "Out of range"}],
        [{"start": True, "title": "Bool start"}],
        [{"start": 0, "title": "  "}],
        [{"start": "0", "title": "String start"}],
    ],
)
def test_invalid_chapters_keep_the_transcript_flat(
    monkeypatch: pytest.MonkeyPatch,
    reply: object,
) -> None:
    chapters = _patch_client(monkeypatch, _FakeClient([reply]))
    assert chapters.chapter_titles(_segments([0, 5])) is None


def test_failed_llm_call_in_any_window_keeps_the_whole_file_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        [[{"start": 0, "title": "Part A"}], RuntimeError("provider down")]
    )
    chapters = _patch_client(monkeypatch, client)
    assert chapters.chapter_titles(_segments([0, 61])) is None
    assert len(client.prompts) == 2


def test_no_segments_has_no_chapters(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([])
    chapters = _patch_client(monkeypatch, client)
    assert chapters.chapter_titles([]) is None
    assert client.prompts == []
