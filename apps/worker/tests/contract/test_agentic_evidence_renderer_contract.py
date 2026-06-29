from shared.services.retrieval.agentic.evidence.renderer import render_leaf_chunks


def test_render_direct_table_chunk_uses_summary_and_asset_url() -> None:
    parts: list[str] = []
    render_leaf_chunks(
        parts,
        [
            {
                "chunk_id": "table-1",
                "chunk_type": "table",
                "content": "<table><tr><td>SHOULD NOT LEAK</td></tr></table>",
                "file_path": "tables/table-企业入驻信息表.html",
                "chunk_metadata": {
                    "summary": "企业入驻信息登记模板",
                    "keywords": ["企业信息", "入驻管理"],
                },
            }
        ],
        "    ",
        asset_lookup={"table-1": "http://localhost:4566/table.html?signature=test"},
    )

    rendered = "\n".join(parts)
    assert "[Table: http://localhost:4566/table.html?signature=test]" in rendered
    assert "企业入驻信息登记模板" in rendered
    assert "企业信息;入驻管理" in rendered
    assert "SHOULD NOT LEAK" not in rendered
    assert "<table" not in rendered


def test_render_direct_table_chunk_does_not_inline_html() -> None:
    parts: list[str] = []
    render_leaf_chunks(
        parts,
        [
            {
                "chunk_id": "table-1",
                "chunk_type": "table",
                "content": "<table><tr><td>企业名称</td></tr></table>",
                "file_path": "tables/table-企业入驻信息表.html",
                "source_chunk_path": "企业信息汇总260509 (1).xlsx/企业批量录入",
                "chunk_metadata": {
                    "summary": "table-企业批量录入\n企业入驻信息登记模板",
                    "keywords": ["企业信息", "入驻管理"],
                },
            }
        ],
        "    ",
        asset_lookup={"table-1": "http://localhost:4566/table.html?signature=test"},
    )

    rendered = "\n".join(parts)
    assert "[Table: http://localhost:4566/table.html?signature=test]" in rendered
    assert "Table path: 企业信息汇总260509 (1).xlsx/企业批量录入" in rendered
    assert "Table asset: tables/table-企业入驻信息表.html" in rendered
    assert "table-企业批量录入" in rendered
    assert "Main columns:" in rendered
    assert "企业信息;入驻管理" in rendered
    assert "<table><tr><td>企业名称</td></tr></table>" not in rendered


def test_render_connected_table_chunk_includes_asset_url() -> None:
    parts: list[str] = []
    render_leaf_chunks(
        parts,
        [
            {
                "chunk_id": "text-1",
                "chunk_type": "text",
                "content": "见表 [tables/table-1.html]",
                "chunk_metadata": {
                    "connect_to": [
                        {
                            "target": "table-1",
                            "ref": "[tables/table-1.html]",
                        }
                    ]
                },
            },
            {
                "chunk_id": "table-1",
                "chunk_type": "table",
                "content": "<table><tr><td>SHOULD NOT LEAK</td></tr></table>",
                "file_path": "tables/table-1.html",
                "chunk_metadata": {"summary": "A 表摘要"},
            },
        ],
        "    ",
        asset_lookup={"table-1": "http://localhost:4566/table-1.html?signature=test"},
    )

    rendered = "\n".join(parts)
    assert "[Table: http://localhost:4566/table-1.html?signature=test]" in rendered
    assert "A 表摘要" in rendered
    assert "SHOULD NOT LEAK" not in rendered
    assert "<table" not in rendered


def test_render_page_chunk_uses_summary_page_nums_and_page_pdf() -> None:
    parts: list[str] = []
    render_leaf_chunks(
        parts,
        [
            {
                "chunk_id": "page-node-1",
                "chunk_type": "page",
                "content": "RAW OCR SHOULD NOT LEAK",
                "chunk_metadata": {
                    "summary": "制度标准总则摘要",
                    "page_nums": [225, 226],
                },
            }
        ],
        "    ",
        asset_lookup={
            "page-node-1": "http://localhost:4566/page_pdfs/225-226.pdf?signature=test"
        },
    )

    rendered = "\n".join(parts)
    assert "Pages 225-226" in rendered
    assert "制度标准总则摘要" in rendered
    assert (
        "Page PDF (pages 225-226): "
        "http://localhost:4566/page_pdfs/225-226.pdf?signature=test"
    ) in rendered
    assert "RAW OCR SHOULD NOT LEAK" not in rendered
