from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.storage.zip_result_service import ZipResultService


def test_page_memory_zip_includes_top_level_doc_profile_and_skips_debug_members(
    tmp_path: Path,
) -> None:
    add_dir = tmp_path / "result"
    add_dir.mkdir()
    (add_dir / "_doc_agent").mkdir()
    (add_dir / "images").mkdir()
    (add_dir / "images" / "fig-1.png").write_bytes(b"png")
    (add_dir / "doc_profile.json").write_text(
        json.dumps({"version": "1.0", "page_count": 2}),
        encoding="utf-8",
    )
    (add_dir / "full.md").write_text("# should not be packed", encoding="utf-8")
    (add_dir / "_doc_agent" / "trace.json").write_text(
        json.dumps({"steps": []}),
        encoding="utf-8",
    )
    (add_dir / "doc_nav.json").write_text(
        json.dumps(
            {
                "version": "1.0",
                "file_name": "demo.pdf",
                "stats": {"total_chunks": 1},
                "sections": [
                    {
                        "title": "Root",
                        "path": "demo.pdf/Root",
                        "level": 1,
                        "summary": "",
                        "chunk_count": 1,
                        "children": [],
                    }
                ],
                "resources": {"images": [], "tables": []},
            }
        ),
        encoding="utf-8",
    )

    chunks = [
        {
            "chunk_id": "node_1",
            "type": "page",
            "content": "body",
            "path": "demo.pdf/Root",
            "metadata": {"page_nums": [1], "summary": "s"},
        },
        {
            "chunk_id": "img_1",
            "type": "image",
            "content": "figure",
            "path": "images/fig-1.png",
            "metadata": {"file_path": "images/fig-1.png", "page_nums": [1]},
        },
    ]

    zip_path, _checksum, statistics, _size = ZipResultService().generate_zip_package(
        job_id="job-pm-zip",
        chunks=chunks,
        add_dir=str(add_dir),
        source_file_name="demo.pdf",
        data_id=None,
        job_metadata={"parse_track": "page_memory", "page_count": 2},
        temp_dir=str(tmp_path),
        parse_track="page_memory",
        zip_file_name="production_result.zip",
    )

    import zipfile

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())

    assert "chunks.json" in names
    assert "doc_nav.json" in names
    assert "manifest.json" in names
    assert "doc_profile.json" in names
    assert "images/fig-1.png" in names
    assert "full.md" not in names
    assert "debug/trace.json" not in names
    assert "debug/anatomy_map.json" not in names
    assert statistics["page_chunks"] == 1
    assert statistics["image_chunks"] == 1


def test_chunk_track_zip_still_packs_optional_full_md(tmp_path: Path) -> None:
    add_dir = tmp_path / "result"
    add_dir.mkdir()
    (add_dir / "full.md").write_text("# keep me", encoding="utf-8")
    (add_dir / "doc_profile.json").write_text(
        json.dumps({"version": "1.0"}),
        encoding="utf-8",
    )

    chunks = [
        {
            "chunk_id": "text_1",
            "type": "text",
            "content": "hello",
            "path": "demo.docx/Root",
            "metadata": {"summary": "s"},
        }
    ]

    zip_path, *_ = ZipResultService().generate_zip_package(
        job_id="job-chunk-zip",
        chunks=chunks,
        add_dir=str(add_dir),
        source_file_name="demo.docx",
        data_id=None,
        job_metadata={"parse_track": "chunk"},
        temp_dir=str(tmp_path),
        parse_track="chunk",
    )

    import zipfile

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())

    assert "full.md" in names
    assert "doc_profile.json" in names
