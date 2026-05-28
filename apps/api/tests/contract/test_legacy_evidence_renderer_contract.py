from shared.services.retrieval.hydration.legacy_evidence import render_legacy_evidence_text


def test_render_legacy_evidence_text_should_group_documents_and_sections() -> None:
    rows = [
        {
            "chunk_id": "c2",
            "content": "second section content",
            "sort_order": 2,
            "source": {
                "source_file_name": "alpha.pdf",
                "section_path": "Alpha / Two",
            },
        },
        {
            "chunk_id": "c1",
            "content": "first section content\nwith more detail",
            "sort_order": 1,
            "source": {
                "source_file_name": "alpha.pdf",
                "section_path": "Alpha / One",
            },
        },
        {
            "chunk_id": "c3",
            "content": "<table><tr><td>metric</td></tr></table>",
            "source_file_name": "beta.pdf",
            "section_path": "Beta / Table",
        },
    ]

    evidence_text = render_legacy_evidence_text(rows)

    assert "[Document] alpha.pdf" in evidence_text
    assert "[Document] beta.pdf" in evidence_text
    assert "▸ Alpha / One" in evidence_text
    assert "▸ Alpha / Two" in evidence_text
    assert "    ┈ first section content" in evidence_text
    assert "    ┈ with more detail" in evidence_text
    assert "    ┈ <table><tr><td>metric</td></tr></table>" in evidence_text
    assert "\u3010\u6587\u6863\u3011" not in evidence_text
    assert "[\u8868\u683c\u5185\u5bb9]" not in evidence_text
    assert "[\u56fe\u7247" not in evidence_text
