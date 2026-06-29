"""Contract tests for cross-page table merge (C5b).

Verifies:
- Column count detection from HTML
- Boundary row extraction (head/tail)
- HTML merge logic (append rows, skip header)
- The top-level merge loop skips non-consecutive pages and mismatched columns
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.page_memory.page_assets import (
    PageAsset,
    _count_html_columns,
    _extract_boundary_rows,
    _merge_table_html_files,
    merge_cross_page_tables,
)


TABLE_4COL = """\
<table border="1" class="dataframe">
  <tbody>
    <tr><td>1.1</td><td>基坑工程</td><td>开挖深度超过3m</td><td>II</td></tr>
    <tr><td>1.2</td><td>基坑工程</td><td>地质条件复杂</td><td>II</td></tr>
    <tr><td>1.3</td><td>基坑工程</td><td>开挖深度超过5m</td><td>I</td></tr>
  </tbody>
</table>
"""

TABLE_4COL_CONTINUATION = """\
<table border="1" class="dataframe">
  <tbody>
    <tr><td>1.4</td><td>基坑工程</td><td>深度虽未超过5m但条件复杂</td><td>I</td></tr>
    <tr><td>2.0</td><td>模板工程</td><td></td><td></td></tr>
    <tr><td>2.1</td><td>模板工程</td><td>搭设高度5m及以上</td><td>II</td></tr>
  </tbody>
</table>
"""

TABLE_3COL = """\
<table border="1" class="dataframe">
  <tbody>
    <tr><td>A</td><td>B</td><td>C</td></tr>
    <tr><td>1</td><td>2</td><td>3</td></tr>
  </tbody>
</table>
"""


def _make_asset(page_index: int, asset_index: int, html_path: str) -> PageAsset:
    return PageAsset(
        asset_id=f"asset_test_{page_index}_{asset_index}",
        page_index=page_index,
        asset_index=asset_index,
        kind="table",
        bbox_px=[0, 0, 100, 100],
        width_px=1000,
        height_px=1000,
        width_pt=612.0,
        height_pt=792.0,
        html_path=html_path,
        html_uri=f"tables/table_page_{page_index}_{asset_index}.html",
        extraction_status="table_html_extracted",
    )


class TestCountHtmlColumns:
    def test_4col(self, tmp_path: Path):
        html_file = tmp_path / "table.html"
        html_file.write_text(TABLE_4COL, encoding="utf-8")
        assert _count_html_columns(str(html_file)) == 4

    def test_3col(self, tmp_path: Path):
        html_file = tmp_path / "table.html"
        html_file.write_text(TABLE_3COL, encoding="utf-8")
        assert _count_html_columns(str(html_file)) == 3

    def test_empty_file(self, tmp_path: Path):
        html_file = tmp_path / "table.html"
        html_file.write_text("", encoding="utf-8")
        assert _count_html_columns(str(html_file)) == 0

    def test_nonexistent_file(self):
        assert _count_html_columns("/nonexistent/path.html") == 0


class TestExtractBoundaryRows:
    def test_tail_rows(self, tmp_path: Path):
        html_file = tmp_path / "table.html"
        html_file.write_text(TABLE_4COL, encoding="utf-8")
        result = _extract_boundary_rows(str(html_file), "tail", max_rows=2)
        assert "1.2" in result
        assert "1.3" in result
        assert "1.1" not in result

    def test_head_rows(self, tmp_path: Path):
        html_file = tmp_path / "table.html"
        html_file.write_text(TABLE_4COL_CONTINUATION, encoding="utf-8")
        result = _extract_boundary_rows(str(html_file), "head", max_rows=2)
        assert "1.4" in result
        assert "2.0" in result
        assert "2.1" not in result

    def test_fewer_rows_than_max(self, tmp_path: Path):
        html_file = tmp_path / "table.html"
        html_file.write_text(TABLE_3COL, encoding="utf-8")
        result = _extract_boundary_rows(str(html_file), "head", max_rows=5)
        assert "A" in result
        assert "1" in result


class TestMergeTableHtmlFiles:
    def test_merge_appends_all_rows(self, tmp_path: Path):
        tail_file = tmp_path / "tail.html"
        head_file = tmp_path / "head.html"
        tail_file.write_text(TABLE_4COL, encoding="utf-8")
        head_file.write_text(TABLE_4COL_CONTINUATION, encoding="utf-8")

        tail_asset = _make_asset(10, 1, str(tail_file))
        head_asset = _make_asset(11, 1, str(head_file))

        _merge_table_html_files(
            tail_asset=tail_asset,
            head_asset=head_asset,
            header_rows_to_skip=0,
        )

        merged_html = tail_file.read_text(encoding="utf-8")
        assert "1.1" in merged_html
        assert "1.3" in merged_html
        assert "1.4" in merged_html
        assert "2.1" in merged_html
        assert not head_file.exists()

    def test_merge_skips_first_row(self, tmp_path: Path):
        tail_file = tmp_path / "tail.html"
        head_file = tmp_path / "head.html"
        tail_file.write_text(TABLE_4COL, encoding="utf-8")
        head_file.write_text(TABLE_4COL_CONTINUATION, encoding="utf-8")

        tail_asset = _make_asset(10, 1, str(tail_file))
        head_asset = _make_asset(11, 1, str(head_file))

        _merge_table_html_files(
            tail_asset=tail_asset,
            head_asset=head_asset,
            header_rows_to_skip=1,
        )

        merged_html = tail_file.read_text(encoding="utf-8")
        assert "1.1" in merged_html
        assert "1.4" not in merged_html  # first row of head skipped
        assert "2.0" in merged_html
        assert "2.1" in merged_html

    def test_column_count_after_merge(self, tmp_path: Path):
        tail_file = tmp_path / "tail.html"
        head_file = tmp_path / "head.html"
        tail_file.write_text(TABLE_4COL, encoding="utf-8")
        head_file.write_text(TABLE_4COL_CONTINUATION, encoding="utf-8")

        tail_asset = _make_asset(10, 1, str(tail_file))
        head_asset = _make_asset(11, 1, str(head_file))

        _merge_table_html_files(
            tail_asset=tail_asset,
            head_asset=head_asset,
            header_rows_to_skip=0,
        )
        assert _count_html_columns(str(tail_file)) == 4


class TestMergeCrossPageTablesSkipsNonConsecutive:
    def test_non_consecutive_pages_skipped(self, tmp_path: Path):
        file_10 = tmp_path / "t10.html"
        file_12 = tmp_path / "t12.html"
        file_10.write_text(TABLE_4COL, encoding="utf-8")
        file_12.write_text(TABLE_4COL_CONTINUATION, encoding="utf-8")

        assets_by_page = {
            10: [_make_asset(10, 1, str(file_10))],
            12: [_make_asset(12, 1, str(file_12))],  # page 11 missing
        }
        result = merge_cross_page_tables(
            assets_by_page=assets_by_page,
            output_dir=str(tmp_path),
            model_name=None,
        )
        assert 10 in result
        assert 12 in result

    def test_mismatched_columns_skipped(self, tmp_path: Path):
        file_10 = tmp_path / "t10.html"
        file_11 = tmp_path / "t11.html"
        file_10.write_text(TABLE_4COL, encoding="utf-8")
        file_11.write_text(TABLE_3COL, encoding="utf-8")

        assets_by_page = {
            10: [_make_asset(10, 1, str(file_10))],
            11: [_make_asset(11, 1, str(file_11))],
        }
        result = merge_cross_page_tables(
            assets_by_page=assets_by_page,
            output_dir=str(tmp_path),
            model_name=None,
        )
        assert 10 in result
        assert 11 in result
