import asyncio
import json

from app.mcp.retrieval_server import to_mcp_query_response
from mcp.types import ImageContent, TextContent
from shared.services.retrieval.execution.response_projection import (
    project_public_retrieval_response,
)
from shared.services.retrieval.execution.routes import _evidence_fields


def test_evidence_fields_group_composed_parts_by_parent() -> None:
    rows = [
        {
            "document_id": "doc_a",
            "source_file_name": "guide.pdf",
            "section_path": "3 诊断 / 3.1",
            "sort_order": 1,
            "composed": [{"type": "text", "text": "first"}],
        },
        {
            "document_id": "doc_a",
            "source_file_name": "guide.pdf",
            "section_path": "3 诊断 / 3.2",
            "sort_order": 2,
            "composed": [
                {"type": "text", "text": "mid "},
                {"type": "image", "media_type": "image/png", "data": "abc"},
            ],
        },
        {
            "document_id": "doc_a",
            "source_file_name": "guide.pdf",
            "section_path": "5 治疗 / 5.1",
            "sort_order": 3,
            "composed": [
                {"type": "text", "text": "<table><tr><td>metric</td></tr></table>"}
            ],
        },
    ]

    fields = _evidence_fields(rows)

    assert fields["evidence"] == [
        {"type": "text", "text": "[E1] [§ guide.pdf / 3 诊断]"},
        {"type": "text", "text": "  first"},
        {"type": "text", "text": "  mid "},
        {"type": "image", "media_type": "image/png", "data": "abc"},
        {"type": "text", "text": "\n"},
        {"type": "text", "text": "[E2] [§ guide.pdf / 5 治疗]"},
        {
            "type": "text",
            "text": "  <table><tr><td>metric</td></tr></table>",
        },
    ]
    assert fields["evidence_text"] == (
        "[E1] [§ guide.pdf / 3 诊断]  first  mid [image: see evidence]"
        "\n[E2] [§ guide.pdf / 5 治疗]  <table><tr><td>metric</td></tr></table>"
    )


def test_mcp_query_response_is_content_blocks_then_json() -> None:
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

    assert response == [
        TextContent(type="text", text="t"),
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "query": "q",
                    "router_used": None,
                    "failure_reason": None,
                    "referenced_chunks": [{"chunk_id": "c1"}],
                    "decision_trace": [{"step": 1}],
                    "results": [{"content": "[images/a.png]"}],
                },
                ensure_ascii=False,
            ),
        ),
    ]
    payload = json.loads(response[-1].text)
    assert "evidence" not in payload
    assert "evidence_text" not in payload
    assert "answer_text" not in payload


def test_mcp_query_response_maps_image_parts_to_image_content() -> None:
    response = to_mcp_query_response(
        {
            "query": "q",
            "router_used": "classic_topk",
            "failure_reason": None,
            "evidence": [
                {"type": "text", "text": "[E1] [§ guide.pdf / 3 诊断]"},
                {"type": "text", "text": "  before"},
                {"type": "image", "media_type": "image/png", "data": "abc"},
                {"type": "text", "text": "\n"},
            ],
            "evidence_text": "[E1] [§ guide.pdf / 3 诊断]  before[image: see evidence]\n",
            "referenced_chunks": [],
            "decision_trace": [],
            "results": [{"content": "raw"}],
        }
    )

    assert response[:-1] == [
        TextContent(type="text", text="[E1] [§ guide.pdf / 3 诊断]"),
        TextContent(type="text", text="  before"),
        ImageContent(type="image", data="abc", mimeType="image/png"),
        TextContent(type="text", text="\n"),
    ]
    payload = json.loads(response[-1].text)
    assert payload == {
        "query": "q",
        "router_used": "classic_topk",
        "failure_reason": None,
        "referenced_chunks": [],
        "decision_trace": [],
        "results": [{"content": "raw"}],
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
