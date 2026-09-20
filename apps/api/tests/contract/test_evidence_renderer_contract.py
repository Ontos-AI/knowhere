from shared.services.retrieval.execution.routes import _evidence_fields


def test_evidence_fields_flatten_composed_parts_in_result_order() -> None:
    rows = [
        {"composed": [{"type": "text", "text": "first"}]},
        {
            "composed": [
                {"type": "text", "text": "mid "},
                {"type": "image", "media_type": "image/png", "data": "abc"},
            ]
        },
        {"composed": [{"type": "text", "text": "<table><tr><td>metric</td></tr></table>"}]},
    ]

    fields = _evidence_fields(rows)

    assert fields["evidence"] == [
        {"type": "text", "text": "first"},
        {"type": "text", "text": "mid "},
        {"type": "image", "media_type": "image/png", "data": "abc"},
        {"type": "text", "text": "<table><tr><td>metric</td></tr></table>"},
    ]
    assert fields["evidence_text"] == (
        "firstmid data:image/png;base64,abc<table><tr><td>metric</td></tr></table>"
    )
