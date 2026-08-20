"""Contract tests for probe.links noise and dest page normalize."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.tools.probe_links import (
    PageLink,
    _link_dest_physical_page,
    annotate_link_noise,
)


def test_link_noise_marks_page_number_header_and_repeated_dest() -> None:
    links = [
        PageLink(
            source_page=2,
            dest_physical_page=10,
            anchor_text="12",
            from_y0=5,
            page_height=100,
        ),
        PageLink(
            source_page=2,
            dest_physical_page=10,
            anchor_text="Intro",
            from_y0=50,
            page_height=100,
        ),
        PageLink(
            source_page=3,
            dest_physical_page=10,
            anchor_text="Intro again",
            from_y0=40,
            page_height=100,
        ),
        PageLink(
            source_page=1,
            dest_physical_page=99,
            anchor_text="Normal title",
            from_y0=80,
            page_height=100,
        ),
    ]
    annotated = annotate_link_noise(links)
    by_anchor = {item["anchor_text"]: item for item in annotated}
    assert "pure_page_number" in by_anchor["12"]["noise"]
    assert "header_zone" in by_anchor["12"]["noise"]
    assert "repeated_dest" in by_anchor["12"]["noise"]
    assert "repeated_dest" in by_anchor["Intro"]["noise"]
    assert by_anchor["Normal title"]["noise"] == []


def test_link_dest_physical_page_by_type() -> None:
    """``int`` is 0-based; digit ``str`` is already 1-based."""
    assert _link_dest_physical_page(6) == 7
    assert _link_dest_physical_page(0) == 1
    assert _link_dest_physical_page("6") == 6
    assert _link_dest_physical_page("7") == 7
