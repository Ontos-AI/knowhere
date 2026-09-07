from shared.services.retrieval.execution.routes import _render_rows_evidence


def test_render_rows_evidence_should_group_by_traceable_path() -> None:
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

    evidence_text = _render_rows_evidence(rows)

    assert "[E1]" in evidence_text
    assert "[E2]" in evidence_text
    assert "[E3]" in evidence_text
    assert "[§ alpha.pdf / Alpha / One]" in evidence_text
    assert "[§ alpha.pdf / Alpha / Two]" in evidence_text
    assert "[§ beta.pdf / Beta / Table]" in evidence_text
    assert "first section content" in evidence_text
    assert "second section content" in evidence_text
    assert "<table><tr><td>metric</td></tr></table>" in evidence_text
    assert "[Document]" not in evidence_text
    assert "▸" not in evidence_text
    assert "┈" not in evidence_text
