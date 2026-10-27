from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.page_memory.memory_service import assemble_c7_node_rows
from app.services.page_memory.page_text_extractor import extract_fine_page_text
from app.services.page_memory.skeleton_extractor import SectionSkeleton
from shared.models.schemas.page_memory_config import PageMemoryConfig


def test_extract_fine_page_text_none_returns_empty_without_transcribe(monkeypatch) -> None:
    calls: list[object] = []

    def boom(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("transcribe must not run when extractor is none")

    monkeypatch.setattr(
        "app.services.page_memory.page_text_extractor.transcribe",
        boom,
    )

    extracted = extract_fine_page_text(
        pdf_path="/tmp/doc.pdf",
        rendered=[SimpleNamespace(page_index=1, image_path="/tmp/p1.png")],
        final_pages=[1, 2],
        extractor="none",
        vlm_model="vision",
        output_dir="/tmp/out",
        concurrency=2,
    )

    assert extracted == {}
    assert calls == []


def test_extract_fine_page_text_vlm_uses_transcribe(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "page-1.png"
    image_path.write_bytes(b"png")

    def fake_transcribe(*, image_paths, model, usage_task):
        assert image_paths == [str(image_path)]
        assert model == "vision"
        assert usage_task == "page_memory.fine_transcribe"
        return "verbatim page one"

    monkeypatch.setattr(
        "app.services.page_memory.page_text_extractor.transcribe",
        fake_transcribe,
    )

    extracted = extract_fine_page_text(
        pdf_path=str(tmp_path / "doc.pdf"),
        rendered=[SimpleNamespace(page_index=1, image_path=str(image_path))],
        final_pages=[1],
        extractor="vlm",
        vlm_model="vision",
        output_dir=str(tmp_path / "out"),
        concurrency=1,
    )

    assert extracted == {1: "verbatim page one"}


def test_extract_fine_page_text_mineru_maps_layout_pages(monkeypatch, tmp_path: Path) -> None:
    from app.services.document_parser.formats.pdf.shard_splitter import LiveShard

    def fake_split_pdf(pdf_path, shards, work_dir, exclude_pages=None):
        assert exclude_pages == {2}
        return [
            LiveShard(
                shard_index=0,
                page_start=1,
                page_end=3,
                page_numbers=[1, 3],
                pdf_path=str(tmp_path / "shard_0.pdf"),
            )
        ]

    def fake_parse_via_full(pdf_path, filename, output_dir, s3_key=None):
        del pdf_path, filename, s3_key
        layout = {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "para_blocks": [
                        {
                            "lines": [
                                {"spans": [{"content": "page one body"}]},
                            ]
                        }
                    ],
                },
                {
                    "page_idx": 1,
                    "para_blocks": [
                        {
                            "lines": [
                                {"spans": [{"content": "page three body"}]},
                            ]
                        }
                    ],
                },
            ]
        }
        Path(output_dir, "layout.json").write_text(
            json.dumps(layout),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        "app.services.document_parser.formats.pdf.shard_splitter.split_pdf",
        fake_split_pdf,
    )
    monkeypatch.setattr(
        "app.services.document_parser.providers.mineru.pdf_service.parse_via_full",
        fake_parse_via_full,
    )

    extracted = extract_fine_page_text(
        pdf_path=str(tmp_path / "doc.pdf"),
        rendered=[],
        final_pages=[1, 3],
        extractor="mineru",
        vlm_model=None,
        output_dir=str(tmp_path / "fine"),
        concurrency=1,
    )

    assert extracted == {1: "page one body", 3: "page three body"}


def test_assemble_c7_prefers_fine_text_over_m0_bands(tmp_path: Path) -> None:
    rows, _pages = assemble_c7_node_rows(
        skeletons=[
            SectionSkeleton(
                title="Root",
                level=0,
                start_page=1,
                end_page=1,
                section_path="doc.pdf/Root",
            )
        ],
        rendered={
            1: SimpleNamespace(
                page_index=1,
                raw_text="m0 band text",
                image_path="",
            )
        },
        tags=[],
        assets_by_page={},
        page_texts={1: "m0 cache text"},
        body_text_by_page={1: "m0 body"},
        page_labels=[],
        toc_pages=[],
        page_count=1,
        output_dir=str(tmp_path),
        filename="doc.pdf",
        verdict="page",
        vlm_model=None,
        page_memory_config=PageMemoryConfig(asset_extraction_enabled=False),
        fine_text_by_page={1: "fine verbatim"},
    )

    page_rows = [row for row in rows if row.get("type") == "page"]
    assert page_rows
    assert "fine verbatim" in page_rows[0]["content"]
    assert "m0 band text" not in page_rows[0]["content"]
