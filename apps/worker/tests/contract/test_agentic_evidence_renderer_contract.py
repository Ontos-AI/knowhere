from shared.services.retrieval.agentic.evidence.renderer import render_leaf_chunks


def test_render_direct_small_table_chunk_includes_asset_url() -> None:
    parts: list[str] = []
    render_leaf_chunks(
        parts,
        [
            {
                "chunk_id": "table-1",
                "chunk_type": "table",
                "content": "<table><tr><td>企业名称</td></tr></table>",
                "file_path": "tables/table-企业入驻信息表.html",
            }
        ],
        "    ",
        asset_lookup={"table-1": "http://localhost:4566/table.html?signature=test"},
    )

    rendered = "\n".join(parts)
    assert "[Table: http://localhost:4566/table.html?signature=test]" in rendered
    assert "<table><tr><td>企业名称</td></tr></table>" in rendered


def test_render_direct_large_table_chunk_uses_asset_stub(monkeypatch) -> None:
    monkeypatch.setenv("RETRIEVAL_AGENTIC_INLINE_TABLE_CHAR_LIMIT", "10")

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
    assert "Large table omitted from evidence_text" in rendered
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
                "content": "<table><tr><td>A</td></tr></table>",
                "file_path": "tables/table-1.html",
            },
        ],
        "    ",
        asset_lookup={"table-1": "http://localhost:4566/table-1.html?signature=test"},
    )

    rendered = "\n".join(parts)
    assert "[Table: http://localhost:4566/table-1.html?signature=test]" in rendered
    assert "<table><tr><td>A</td></tr></table>" in rendered
