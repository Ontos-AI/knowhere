from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from PIL import Image

from app.services.page_memory import page_assets
from app.services.page_memory.page_renderer import PageRenderResult


def test_page_assets_default_to_qwen_flash_and_full_page_scan(monkeypatch) -> None:
    monkeypatch.delenv("PAGE_MEMORY_ASSET_MODEL", raising=False)
    monkeypatch.delenv("PAGE_MEMORY_ASSET_MAX_PAGES", raising=False)

    assert page_assets.get_asset_model() == "qwen3.6-flash"
    assert page_assets.get_asset_max_pages(301) == 301


def test_page_asset_detection_uses_lowest_temperature(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    class _FakeClient:
        def chat_completion_with_usage(self, **kwargs):
            captured.update(kwargs)
            return (
                json.dumps(
                    {
                        "regions": [
                            {
                                "kind": "table",
                                "bbox": [100, 100, 300, 300],
                                "title": "table title",
                                "confidence": 1.0,
                            }
                        ]
                    }
                ),
                {"total_tokens": 10},
            )

    import shared.services.ai.openai_compatible_client_sync as client_mod

    monkeypatch.setattr(
        client_mod, "get_openai_client", lambda model=None: _FakeClient()
    )

    image_path = tmp_path / "page.png"
    Image.new("RGB", (100, 120), "white").save(image_path)
    page = PageRenderResult(
        page_index=1,
        image_path=str(image_path),
        raw_text="",
        width=50,
        height=60,
        is_landscape=False,
    )

    assets = page_assets.detect_page_assets(
        page=page,
        source_name="demo.pdf",
        model_name=page_assets.get_asset_model(),
        budget=None,
        confidence_threshold=0.3,
    )

    prompt = captured["messages"][0]["content"][0]["text"]  # type: ignore[index]
    assert captured["model"] == "qwen3.6-flash"
    assert captured["temperature"] == 0
    assert assets[0].bbox_px == [10, 12, 30, 36]
    assert assets[0].title == "table title"
    assert assets[0].caption == ""
    assert '"kind": "table|figure"' in prompt
    assert '"caption"' not in prompt
    assert '"summary"' not in prompt
    assert '"keywords"' not in prompt
    assert '"table|chart|figure"' not in prompt


def test_page_asset_extraction_keeps_debug_output_minimal(
    monkeypatch, tmp_path
) -> None:
    page_images = []
    for page_index in [1, 2]:
        image_path = tmp_path / f"page-{page_index}.png"
        Image.new("RGB", (100, 120), "white").save(image_path)
        page_images.append(
            PageRenderResult(
                page_index=page_index,
                image_path=str(image_path),
                raw_text="",
                width=50,
                height=60,
                is_landscape=False,
            )
        )

    def _fake_detect(**kwargs):
        page = kwargs["page"]
        if page.page_index != 1:
            return []
        return [
            page_assets.PageAsset(
                asset_id="asset_1",
                page_index=1,
                asset_index=1,
                kind="table",
                bbox_px=[10, 12, 30, 36],
                width_px=100,
                height_px=120,
                width_pt=50,
                height_pt=60,
                title="table",
                summary="summary",
                confidence=1.0,
            )
        ]

    def _fake_crop(*, asset, page_image_path, output_dir, margin_px=4):
        asset.image_uri = "images/image_page_1_table_1.png"
        asset.image_path = str(tmp_path / asset.image_uri)
        return asset

    def _fake_extract_table(*, asset, pdf_path, output_dir):
        table_path = tmp_path / "tables" / "table_page_1_1.html"
        table_path.parent.mkdir(parents=True, exist_ok=True)
        table_path.write_text("<table><tr><td>A</td></tr></table>", encoding="utf-8")
        asset.html_uri = "tables/table_page_1_1.html"
        asset.html_path = str(table_path)
        asset.extraction_status = "table_html_extracted"
        return asset

    monkeypatch.setattr(page_assets, "detect_page_assets", _fake_detect)
    monkeypatch.setattr(page_assets, "crop_page_asset", _fake_crop)
    monkeypatch.setattr(page_assets, "extract_table_html", _fake_extract_table)

    assets_by_page = page_assets.extract_page_assets_from_renders(
        pdf_path="demo.pdf",
        rendered_pages=page_images,
        output_dir=str(tmp_path),
        model_name=page_assets.get_asset_model(),
        budget=None,
        max_pages=2,
        confidence_threshold=0.3,
    )

    assert sorted(assets_by_page) == [1]
    assert (tmp_path / "tables" / "table_page_1_1.html").exists()
    assert (tmp_path / "asset_annotate" / "page_1.png").exists()
    assert not (tmp_path / "page_asset_bboxes.json").exists()


def test_page_assets_adds_java_home_to_path(monkeypatch, tmp_path) -> None:
    java_home = tmp_path / "jdk"
    java_bin = java_home / "bin"
    java_bin.mkdir(parents=True)
    java = java_bin / "java"
    java.write_text(
        "#!/bin/sh\n"
        "echo 'openjdk version \"25.0.2\"' >&2\n"
        "exit 0\n",
        encoding="utf-8",
    )
    java.chmod(0o755)

    monkeypatch.setenv("JAVA_HOME", str(java_home))
    monkeypatch.setenv("PATH", os.defpath)

    assert page_assets._has_working_java() is True  # noqa: SLF001
    assert os.environ["PATH"].split(os.pathsep)[0] == str(java_bin)
