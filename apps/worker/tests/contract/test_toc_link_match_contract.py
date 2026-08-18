"""Contract tests for PDF link destination page normalization."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.structure.toc_link_enrichment import (
    _link_dest_physical_page,
)


def test_link_dest_physical_page_by_type() -> None:
    """``int`` is 0-based; digit ``str`` is already 1-based."""
    assert _link_dest_physical_page(6) == 7
    assert _link_dest_physical_page(0) == 1
    assert _link_dest_physical_page("6") == 6
    assert _link_dest_physical_page("7") == 7
