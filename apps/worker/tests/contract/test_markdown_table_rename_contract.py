from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_parser.formats.markdown.deferred_summary import (  # noqa: E402
    replace_chunk_ref_in_rows,
    _apply_table_summary_result,
)
from app.services.document_parser.formats.markdown.deferred_task import (  # noqa: E402
    TableDeferredSummaryTask,
)
from app.services.document_parser.formats.markdown.table_asset import (  # noqa: E402
    MarkdownTableAssetRequest,
    build_markdown_table_asset,
)
from shared.services.ai.summary.model import AssetSummary, Entity  # noqa: E402
from shared.utils.chunk_refs import build_chunk_ref  # noqa: E402


def test_replace_chunk_ref_updates_bare_table_content_and_bracketed_text() -> None:
    old_path = "tables/table-12 流程名称 招标文件.html"
    new_path = "tables/table-12 招标文件及议标项目评审流程图.html"
    rows: list[list[str | int]] = [
        [old_path, old_path, "table", 10, "", "table-12", "id", "", ""],
        [
            f"see {build_chunk_ref(old_path)}",
            "doc/section",
            "ptxt",
            20,
            "",
            "",
            "text-id",
            "",
            "",
        ],
    ]

    replace_chunk_ref_in_rows(rows, old_path, new_path)

    assert rows[0][0] == new_path
    assert rows[0][1] == new_path
    assert rows[1][0] == f"see {build_chunk_ref(new_path)}"


def test_table_deferred_task_has_no_legacy_count_field(tmp_path: Path) -> None:
    asset = build_markdown_table_asset(
        MarkdownTableAssetRequest(
            table_html="<table><tr><td>流程名称</td><td>招标文件</td></tr></table>",
            table_dir=str(tmp_path),
            table_count=12,
            timestamp="2026-07-21 00:00:00",
            summary_table=True,
            row_index=0,
        )
    )

    assert asset.deferred_task is not None
    assert isinstance(asset.deferred_task, TableDeferredSummaryTask)
    assert not hasattr(asset.deferred_task, "table_count")
    assert asset.deferred_task.table_name.startswith("table-12 ")


def test_apply_table_summary_rename_keeps_index_and_syncs_content(
    tmp_path: Path,
) -> None:
    old_stem = "table-12 流程名称 招标文件及议标项目评审流程图 流程编号"
    old_relative = f"tables/{old_stem}.html"
    old_file = tmp_path / f"{old_stem}.html"
    old_file.write_text("<table><tr><td>x</td></tr></table>", encoding="utf-8")

    text_content = f"\n{build_chunk_ref(old_relative)}\n"
    rows: list[list[str | int]] = [
        [old_relative, old_relative, "table", len(old_relative), "", "table-12", "t", "", ""],
        [text_content, "doc/3、招标文件评审流程运行图", "ptxt", len(text_content), "", "", "x", "", ""],
    ]
    task = TableDeferredSummaryTask(
        row_index=0,
        table_html="<table><tr><td>x</td></tr></table>",
        table_dir=str(tmp_path),
        table_name=old_stem,
    )

    _apply_table_summary_result(
        rows,
        task,
        0,
        AssetSummary(
            title="招标文件及议标项目评审流程图",
            summary="三类风险招标文件评审流程",
            entities=[Entity(text="市场开发部", type="org")],
            kind="table",
        ),
    )

    new_relative = str(rows[0][1])
    assert new_relative.startswith("tables/table-12 ")
    assert "招标文件及议标项目评审流程图" in new_relative
    assert rows[0][0] == new_relative
    assert build_chunk_ref(new_relative) in str(rows[1][0])
    assert not old_file.exists()
    assert (tmp_path / Path(new_relative).name).exists()
