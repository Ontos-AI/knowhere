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
from app.services.page_memory.skeleton_extractor import SectionSkeleton
from shared.models.schemas.page_memory_config import PageMemoryConfig


def _live_extractor():
    import app.services.page_memory.page_text_extractor as extractor

    return extractor


def test_extract_fine_page_text_none_returns_empty_without_transcribe(monkeypatch) -> None:
    extractor = _live_extractor()
    calls: list[object] = []

    def boom(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("transcribe must not run when extractor is none")

    monkeypatch.setattr(extractor, "transcribe", boom)

    extracted = extractor.extract_fine_page_text(
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
    extractor = _live_extractor()
    image_path = tmp_path / "page-1.png"
    image_path.write_bytes(b"png")

    def fake_transcribe(*, image_paths, model, usage_task):
        assert image_paths == [str(image_path)]
        assert model == "vision"
        assert usage_task == "page_memory.fine_transcribe"
        return "verbatim page one"

    monkeypatch.setattr(extractor, "transcribe", fake_transcribe)

    extracted = extractor.extract_fine_page_text(
        pdf_path=str(tmp_path / "doc.pdf"),
        rendered=[SimpleNamespace(page_index=1, image_path=str(image_path))],
        final_pages=[1],
        extractor="vlm",
        vlm_model="vision",
        output_dir=str(tmp_path / "out"),
        concurrency=1,
    )

    assert extracted == {1: "verbatim page one"}


def test_extract_fine_page_text_mineru_maps_layout_pages_and_tables(
    monkeypatch, tmp_path: Path
) -> None:
    extractor = _live_extractor()
    from app.services.document_parser.formats.pdf import shard_splitter
    from app.services.document_parser.formats.pdf.shard_splitter import LiveShard
    from app.services.document_parser.providers.mineru import pdf_service

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
                        },
                        {
                            "type": "table",
                            "blocks": [
                                {
                                    "type": "table_body",
                                    "lines": [
                                        {
                                            "spans": [
                                                {
                                                    "type": "table",
                                                    "html": "<table><tr><th>Op</th><th>Cycles</th></tr><tr><td>LDA</td><td>2</td></tr></table>",
                                                }
                                            ]
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
            ]
        }
        Path(output_dir, "layout.json").write_text(
            json.dumps(layout),
            encoding="utf-8",
        )

    monkeypatch.setattr(shard_splitter, "split_pdf", fake_split_pdf)
    monkeypatch.setattr(pdf_service, "parse_via_full", fake_parse_via_full)

    extracted = extractor.extract_fine_page_text(
        pdf_path=str(tmp_path / "doc.pdf"),
        rendered=[],
        final_pages=[1, 3],
        extractor="mineru",
        vlm_model=None,
        output_dir=str(tmp_path / "fine"),
        concurrency=1,
    )

    assert extracted[1] == "page one body"
    assert "page three body" in extracted[3]
    assert "Op | Cycles" in extracted[3]
    assert "LDA | 2" in extracted[3]
    assert not (tmp_path / "fine").exists()


def test_extract_fine_page_text_ocr_uses_ocr_rendered_pages(
    monkeypatch, tmp_path: Path
) -> None:
    extractor = _live_extractor()
    image_path = tmp_path / "page-1.png"
    image_path.write_bytes(b"png")

    def fake_ocr(image_path_by_page):
        assert image_path_by_page == {1: str(image_path)}
        return {1: "rapidocr page one"}

    monkeypatch.setattr(extractor, "ocr_rendered_pages", fake_ocr)

    extracted = extractor.extract_fine_page_text(
        pdf_path=str(tmp_path / "doc.pdf"),
        rendered=[SimpleNamespace(page_index=1, image_path=str(image_path))],
        final_pages=[1],
        extractor="ocr",
        vlm_model="vision",
        output_dir=str(tmp_path / "out"),
        concurrency=1,
    )

    assert extracted == {1: "rapidocr page one"}


def test_extract_fine_page_text_vlm_falls_back_to_ocr_only_on_exception(
    monkeypatch, tmp_path: Path
) -> None:
    extractor = _live_extractor()
    p1 = tmp_path / "page-1.png"
    p2 = tmp_path / "page-2.png"
    p1.write_bytes(b"png1")
    p2.write_bytes(b"png2")

    def fake_transcribe(*, image_paths, model, usage_task):
        del model, usage_task
        if image_paths == [str(p1)]:
            raise RuntimeError("vlm rate limit on page 1")
        return ""

    ocr_calls: list[dict[int, str]] = []

    def fake_ocr(image_path_by_page):
        ocr_calls.append(dict(image_path_by_page))
        return {1: "ocr recovered page 1"}

    monkeypatch.setattr(extractor, "transcribe", fake_transcribe)
    monkeypatch.setattr(extractor, "ocr_rendered_pages", fake_ocr)

    extracted = extractor.extract_fine_page_text(
        pdf_path=str(tmp_path / "doc.pdf"),
        rendered=[
            SimpleNamespace(page_index=1, image_path=str(p1)),
            SimpleNamespace(page_index=2, image_path=str(p2)),
        ],
        final_pages=[1, 2],
        extractor="vlm",
        vlm_model="vision",
        output_dir=str(tmp_path / "out"),
        concurrency=2,
    )

    assert extracted == {1: "ocr recovered page 1"}
    assert ocr_calls == [{1: str(p1)}]


def test_extract_fine_page_text_mineru_failure_falls_back_to_ocr_then_m0(
    monkeypatch, tmp_path: Path
) -> None:
    extractor = _live_extractor()
    from app.services.document_parser.formats.pdf import shard_splitter

    vlm_calls: list[object] = []

    def boom(*_args, **_kwargs):
        vlm_calls.append(True)
        raise AssertionError("transcribe must not run when mineru falls back to ocr")

    monkeypatch.setattr(extractor, "transcribe", boom)
    monkeypatch.setattr(
        shard_splitter,
        "split_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("split failed")),
    )

    ocr_calls: list[dict[int, str]] = []

    def fake_ocr(image_path_by_page):
        ocr_calls.append(dict(image_path_by_page))
        return {1: "ocr fallback from mineru"}

    monkeypatch.setattr(extractor, "ocr_rendered_pages", fake_ocr)

    extracted = extractor.extract_fine_page_text(
        pdf_path=str(tmp_path / "doc.pdf"),
        rendered=[SimpleNamespace(page_index=1, image_path="/tmp/p1.png")],
        final_pages=[1],
        extractor="mineru",
        vlm_model="vision",
        output_dir=str(tmp_path / "fine"),
        concurrency=1,
    )

    assert extracted == {1: "ocr fallback from mineru"}
    assert ocr_calls == [{1: "/tmp/p1.png"}]
    assert vlm_calls == []
    assert not (tmp_path / "fine").exists()

    monkeypatch.setattr(
        extractor,
        "ocr_rendered_pages",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("ocr also failed")),
    )
    extracted_after_both_fail = extractor.extract_fine_page_text(
        pdf_path=str(tmp_path / "doc.pdf"),
        rendered=[SimpleNamespace(page_index=1, image_path="/tmp/p1.png")],
        final_pages=[1],
        extractor="mineru",
        vlm_model="vision",
        output_dir=str(tmp_path / "fine2"),
        concurrency=1,
    )
    assert extracted_after_both_fail == {}
    assert not (tmp_path / "fine2").exists()


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
