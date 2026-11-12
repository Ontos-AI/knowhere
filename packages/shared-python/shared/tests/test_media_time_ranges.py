from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.chunks.time_ranges import (  # noqa: E402
    format_clock_ms,
    format_time_ranges,
    section_time_ranges,
    valid_time_ranges,
    widen_time_span,
)
from shared.services.retrieval.agent_tools.snippet import build_row, format_row  # noqa: E402
from shared.services.retrieval.publication_content import (  # noqa: E402
    DocumentSectionPublisher,
)
from shared.services.retrieval.publication_models import (  # noqa: E402
    DocumentPublicationScope,
)


def test_clock_and_labels() -> None:
    assert format_clock_ms(0) == "00:00:00"
    assert format_clock_ms(3_723_999) == "01:02:03"
    assert format_time_ranges([{"start_ms": 723_000, "end_ms": 860_000}]) == (
        "[00:12:03–00:14:20]"
    )
    assert format_time_ranges(None) == ""
    assert format_time_ranges([{"start_ms": "1", "end_ms": 2}]) == ""


def test_valid_time_ranges_drops_malformed_entries() -> None:
    assert valid_time_ranges(
        [
            {"start_ms": 1, "end_ms": 2},
            {"start_ms": 5, "end_ms": 4},
            {"start_ms": True, "end_ms": 2},
            "x",
        ]
    ) == [{"start_ms": 1, "end_ms": 2}]


def test_widen_time_span_covers_all_ranges() -> None:
    assert widen_time_span(None, [{"start_ms": 10, "end_ms": 20}]) == [
        {"start_ms": 10, "end_ms": 20}
    ]
    assert widen_time_span(
        [{"start_ms": 10, "end_ms": 20}], [{"start_ms": 5, "end_ms": 15}]
    ) == [{"start_ms": 5, "end_ms": 20}]
    assert widen_time_span(None, None) == []


def test_outline_row_renders_time_span_before_title() -> None:
    row = build_row(
        kind="section",
        document_id="doc",
        section_path="talk.mp3 / Pricing",
        title="Pricing",
        time_ranges=[{"start_ms": 61_000, "end_ms": 125_000}],
    )
    assert format_row(row).startswith("- [section] [00:01:01–00:02:05] Pricing |")
    plain = build_row(kind="section", document_id="doc", section_path="a", title="A")
    assert plain["time_ranges"] == []
    assert format_row(plain).startswith("- [section] A |")


class _RecordingSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)


def test_publisher_widens_section_and_ancestor_time_spans() -> None:
    publisher = DocumentSectionPublisher(
        db=_RecordingSession(),  # type: ignore[arg-type]
        scope=DocumentPublicationScope(
            user_id="u1",
            namespace="default",
            document_id="doc",
            job_result_id="rev",
            source_file_name="talk.mp3",
        ),
        preload_existing=False,
    )
    publisher.ensure_section(
        ["Part / One", "Intro"], time_ranges=[{"start_ms": 0, "end_ms": 1_000}]
    )
    publisher.ensure_section(
        ["Part / One", "Pricing"], time_ranges=[{"start_ms": 1_000, "end_ms": 5_000}]
    )
    publisher.ensure_section(["Part / One", "Text only"])
    sections = publisher._sections_by_path
    assert section_time_ranges(sections["Part / One"].section_metadata) == [
        {"start_ms": 0, "end_ms": 5_000}
    ]
    assert section_time_ranges(sections["Part / One / Intro"].section_metadata) == [
        {"start_ms": 0, "end_ms": 1_000}
    ]
    assert section_time_ranges(sections["Part / One / Text only"].section_metadata) == []
