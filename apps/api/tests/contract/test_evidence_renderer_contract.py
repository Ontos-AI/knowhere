import asyncio

from app.mcp.retrieval_server import to_mcp_query_response
from shared.services.retrieval.execution.response_projection import (
    project_public_retrieval_response,
)
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


def test_mcp_query_response_keeps_evidence_and_debug_results() -> None:
    response = to_mcp_query_response(
        {
            "query": "q",
            "evidence": [{"type": "text", "text": "t"}],
            "evidence_text": "t",
            "results": [
                {
                    "content": "[images/a.png]",
                }
            ],
            "referenced_chunks": [{"chunk_id": "c1"}],
            "decision_trace": [{"step": 1}],
            "answer_text": "should drop",
        }
    )

    assert response == {
        "query": "q",
        "evidence": [{"type": "text", "text": "t"}],
        "evidence_text": "t",
        "results": [
            {
                "content": "[images/a.png]",
            }
        ],
        "referenced_chunks": [{"chunk_id": "c1"}],
        "decision_trace": [{"step": 1}],
    }


def test_public_results_keep_placeholders_without_composed() -> None:
    public = asyncio.run(
        project_public_retrieval_response(
            {
                "namespace": "default",
                "query": "q",
                "router_used": "classic",
                "evidence": [{"type": "text", "text": "<table>Q4</table>"}],
                "evidence_text": "<table>Q4</table>",
                "results": [
                    {
                        "chunk_id": "c1",
                        "chunk_type": "text",
                        "content": "见表 [tables/a.html]",
                        "composed": [{"type": "text", "text": "见表 <table>Q4</table>"}],
                        "score": 1,
                        "document_id": "d1",
                    }
                ],
            }
        )
    )

    assert public["evidence"] == [{"type": "text", "text": "<table>Q4</table>"}]
    assert public["results"][0]["content"] == "见表 [tables/a.html]"
    assert "composed" not in public["results"][0]
