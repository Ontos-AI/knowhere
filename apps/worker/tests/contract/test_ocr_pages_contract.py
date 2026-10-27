"""ocr_rendered_pages runs RapidOCR on rendered page PNGs and isolates page errors."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.tools.ocr_pages import ocr_rendered_pages


def test_ocr_rendered_pages_extracts_joined_text() -> None:
    calls: list[tuple[int, str, int]] = []

    class FakeRunner:
        def run_page(self, page: int, image_path: str, *, timeout: int):
            calls.append((page, image_path, timeout))
            return {
                "ok": True,
                "page": page,
                "lines": [
                    {
                        "box": [[0, 0], [1, 0], [1, 1], [0, 1]],
                        "text": "Hello",
                        "score": 0.9,
                    },
                    {
                        "box": [[0, 2], [1, 2], [1, 3], [0, 3]],
                        "text": "World",
                        "score": 0.95,
                    },
                ],
            }

    with patch.dict(ocr_rendered_pages.__globals__, {"_OCR_RUNNER": FakeRunner()}):
        extracted = ocr_rendered_pages({1: "/tmp/p1.png"})

    assert extracted == {1: "Hello\nWorld"}
    assert calls == [(1, "/tmp/p1.png", 1800)]


def test_ocr_rendered_pages_isolates_single_page_failure() -> None:
    calls: list[tuple[int, str]] = []

    class FakeRunner:
        def run_page(self, page: int, image_path: str, *, timeout: int):
            assert timeout == 1800
            calls.append((page, image_path))
            if page == 1:
                raise RuntimeError("ocr runner crashed on page 1")
            return {
                "ok": True,
                "page": page,
                "lines": [{"text": "Page 2 text"}],
            }

    with patch.dict(ocr_rendered_pages.__globals__, {"_OCR_RUNNER": FakeRunner()}):
        extracted = ocr_rendered_pages({1: "/tmp/p1.png", 2: "/tmp/p2.png"})

    assert extracted == {2: "Page 2 text"}
    assert calls == [(1, "/tmp/p1.png"), (2, "/tmp/p2.png")]
