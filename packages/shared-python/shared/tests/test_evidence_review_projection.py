"""Review only the composed, selected packet and preserve source identity."""

from __future__ import annotations

import asyncio
import copy
import os
import threading
from dataclasses import replace
from typing import Any, cast

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agent_explore.evidence_pool import (
    Candidate,
    EvidencePool,
    compose_pool_evidence,
)
from shared.services.retrieval.agent_tools import ToolResult
from shared.services.retrieval.execution.evidence_review import (
    assemble_review_rows,
    build_review_snapshot,
    review_sources,
)
from shared.services.retrieval.execution.query_request import RetrievalQuery


def _row(**changes: Any) -> dict[str, Any]:
    return {
        "document_id": "doc_alpha",
        "chunk_id": "chunk_capacity",
        "job_result_id": "revision_1",
        "source_file_name": "alpha.pdf",
        "section_path": "Launch/Capacity",
        "chunk_type": "text",
        "content": "The raw body is not the composed packet.",
        "section_summary": "Incorrect summary: 100 tonnes.",
        "chunk_metadata": {"page_nums": [4], "summary": "Another wrong summary."},
        "sort_order": 0,
        "composed": [{"type": "text", "text": "Payload: 23 tonnes to LEO."}],
        **changes,
    }


def _pick(row: dict[str, Any], **changes: Any) -> Candidate:
    return Candidate(
        **{
            "handle": "R1.1",
            "kind": "read",
            "document_id": row["document_id"],
            "source_file_name": row["source_file_name"],
            "section_path": row["section_path"],
            "chunk_ids": (row["chunk_id"],),
            "summary": "Executor claims every requirement is satisfied.",
            **changes,
        }
    )


def test_snapshot_uses_selected_composed_text_without_summary_or_unpicked_support() -> None:
    selected = _row()
    unpicked = _row(
        chunk_id="chunk_unpicked",
        composed=[{"type": "text", "text": "Unpicked payload: 100 tonnes."}],
    )
    pool = [_pick(selected)]
    evidence = compose_pool_evidence(pool, [selected, unpicked])

    snapshot = build_review_snapshot(
        query="What is the payload to LEO?", pool=pool,
        rows=[selected, unpicked], evidence=evidence,
    )

    assert len(snapshot.items) == 1
    item = snapshot.items[0]
    assert item.text == "Payload: 23 tonnes to LEO."
    assert item.document_id == "doc_alpha"
    assert item.chunk_id == "chunk_capacity"
    assert item.revision == "revision_1"
    assert item.page_nums == (4,)
    assert snapshot.limitation is None
    assert review_sources(snapshot) == [{
        "evidence_id": item.evidence_id,
        "document_id": "doc_alpha", "chunk_id": "chunk_capacity",
        "revision": "revision_1", "page_nums": (4,),
        "section_path": "Launch/Capacity", "source_file_name": "alpha.pdf",
    }]


@pytest.mark.asyncio
async def test_shared_chunk_hashes_keep_document_specific_composed_tables() -> None:
    rows: list[dict[str, Any]] = []
    pool: list[Candidate] = []
    queried_tables: dict[tuple[str, str], str] = {}
    for document_id, tonnes in (("doc_alpha", 23), ("doc_beta", 64)):
        body = _row(
            document_id=document_id,
            source_file_name=f"{document_id}.pdf",
            content="Payload data: [tables/capacity.html]",
            chunk_metadata={"connect_to": [{
                "target": "shared_table_hash", "relation": "embeds",
                "ref": "[tables/capacity.html]",
            }]},
        )
        table = _row(
            document_id=document_id, chunk_id="shared_table_hash",
            chunk_type="table", content="Wrong cached table description.",
            source_file_name=f"{document_id}.pdf", sort_order=1,
            chunk_metadata={"summary": "Wrong table summary: 100 tonnes."},
        )
        rows.extend([body, table])
        pool.append(_pick(body, handle=f"R1.{len(pool) + 1}"))
        queried_tables[(document_id, "shared_table_hash")] = (
            f"<table><tr><td>LEO</td><td>{tonnes} tonnes</td></tr></table>"
        )
    context = RetrievalQuery.from_parameters(
        db=cast(AsyncSession, None), user_id="reader", namespace="default",
        query="Compare payloads", top_k=2, exclude_document_ids=[],
        exclude_sections=[], review_evidence=True,
    ).build_route_context()

    assembled = await assemble_review_rows(
        context=context, db=context.db, rows=rows, queried_tables=queried_tables,
    )
    evidence = compose_pool_evidence(pool, assembled)
    snapshot = build_review_snapshot(
        query=context.query, pool=pool, rows=assembled, evidence=evidence,
    )

    assert len(assembled) == len(snapshot.items) == 2
    assert len({item.evidence_id for item in snapshot.items}) == 2
    text_by_document = {item.document_id: item.text for item in snapshot.items}
    assert "23 tonnes" in text_by_document["doc_alpha"]
    assert "64 tonnes" not in text_by_document["doc_alpha"]
    assert "64 tonnes" in text_by_document["doc_beta"]
    assert "23 tonnes" not in text_by_document["doc_beta"]
    assert all("100 tonnes" not in item.text for item in snapshot.items)
    delivered = "\n".join(part.get("text", "") for part in evidence)
    assert "23 tonnes" in delivered and "64 tonnes" in delivered


def test_page_snapshot_never_substitutes_raw_body_for_delivered_summary_and_image() -> None:
    page = _row(
        chunk_type="page",
        content="Raw OCR claims payload is 100 tonnes.",
        composed=[
            {"type": "text", "text": "A chart compares launch vehicles."},
            {"type": "image", "media_type": "image/png", "data": "cGFnZQ=="},
        ],
    )
    pool = [_pick(page)]
    snapshot = build_review_snapshot(
        query="What is the exact payload?", pool=pool, rows=[page],
        evidence=compose_pool_evidence(pool, [page]),
    )

    assert snapshot.items[0].text == "A chart compares launch vehicles."
    assert snapshot.limitation is not None
    assert "100 tonnes" not in snapshot.items[0].text


def test_outline_is_delivered_context_but_not_factual_support() -> None:
    row = _row()
    outline = _pick(
        row, kind="outline", outline_lines=("Payload capabilities",),
    )
    evidence = compose_pool_evidence([outline], [row])
    snapshot = build_review_snapshot(
        query="What is the payload?", pool=[outline], rows=[row], evidence=evidence,
    )

    assert "Payload capabilities" in str(evidence)
    assert snapshot.items == ()
    changed_outline = replace(outline, outline_lines=("Payload capabilities, revised",))
    changed = build_review_snapshot(
        query="What is the payload?", pool=[changed_outline], rows=[row],
        evidence=compose_pool_evidence([changed_outline], [row]),
    )
    assert changed.fingerprint != snapshot.fingerprint


def test_read_issue_pick_and_compose_keep_same_hash_document_provenance() -> None:
    rows = [
        _row(document_id="doc_alpha", source_file_name="alpha.pdf", section_path="Alpha/Capacity"),
        _row(
            document_id="doc_beta", source_file_name="beta.pdf", section_path="Beta/Capacity",
            composed=[{"type": "text", "text": "Payload: 64 tonnes to LEO."}],
        ),
    ]
    pool = EvidencePool()
    pool.begin_round()
    issued = pool.issue("corpus.read", ToolResult(
        text="Both reads succeeded.", payload={
            "refs": [{
                "status": "ok", "document_id": row["document_id"],
                "chunk_ids": [row["chunk_id"]],
            } for row in rows],
            "chunks": rows,
        },
    ))
    pool.begin_round()
    picked = pool.apply_pick([candidate.handle for candidate in issued])
    evidence = compose_pool_evidence(pool.entries, rows)
    snapshot = build_review_snapshot(query="Compare payloads", pool=pool.entries, rows=rows, evidence=evidence)

    assert len(picked.added) == 2 and not picked.rejected
    assert [(candidate.document_id, candidate.source_file_name, candidate.section_path) for candidate in pool.entries] == [
        ("doc_alpha", "alpha.pdf", "Alpha/Capacity"),
        ("doc_beta", "beta.pdf", "Beta/Capacity"),
    ]
    delivered = "\n".join(part.get("text", "") for part in evidence)
    assert "[§ alpha.pdf / Alpha]" in delivered
    assert "[§ beta.pdf / Beta]" in delivered
    assert {item.document_id: item.text for item in snapshot.items} == {
        "doc_alpha": "Payload: 23 tonnes to LEO.",
        "doc_beta": "Payload: 64 tonnes to LEO.",
    }


@pytest.mark.asyncio
async def test_review_hydration_can_be_cancelled_while_sync_asset_read_is_blocked(monkeypatch) -> None:
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_composition(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(timeout=3), "The test must release the asset worker"
            return [{"type": "text", "text": "Loaded asset"}]
        finally:
            finished.set()

    monkeypatch.setattr(
        "shared.services.retrieval.hydration.result_assembly.compose_evidence_parts",
        blocked_composition,
    )
    context = RetrievalQuery.from_parameters(
        db=cast(AsyncSession, None), user_id="reader", namespace="default",
        query="Payload?", top_k=1, exclude_document_ids=[], exclude_sections=[],
        review_evidence=True,
    ).build_route_context()
    task = asyncio.create_task(assemble_review_rows(
        context=context, db=context.db, rows=[_row()], queried_tables={},
    ))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert not finished.is_set()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await asyncio.gather(task, return_exceptions=True)


def test_fingerprint_binds_query_revision_text_and_entire_delivered_media_packet() -> None:
    row = _row(composed=[
        {"type": "text", "text": "Payload: 23 tonnes to LEO."},
        {"type": "image", "media_type": "image/png", "data": "b2xk"},
    ])
    pool = [_pick(row)]
    evidence = compose_pool_evidence(pool, [row])
    baseline = build_review_snapshot(query="Payload?", pool=pool, rows=[row], evidence=evidence)
    changed_text = copy.deepcopy(row)
    changed_text["composed"][0]["text"] = "Payload: 64 tonnes to LEO."
    changed_media = copy.deepcopy(evidence)
    next(part for part in changed_media if part["type"] == "image")["data"] = "bmV3"

    changed_snapshots = [
        build_review_snapshot(query="Payload to GEO?", pool=pool, rows=[row], evidence=evidence),
        build_review_snapshot(query="Payload?", pool=pool, rows=[{**row, "job_result_id": "revision_2"}], evidence=evidence),
        build_review_snapshot(query="Payload?", pool=pool, rows=[changed_text], evidence=compose_pool_evidence(pool, [changed_text])),
        build_review_snapshot(query="Payload?", pool=pool, rows=[row], evidence=changed_media),
    ]
    assert all(item.fingerprint != baseline.fingerprint for item in changed_snapshots)
    assert build_review_snapshot(
        query="Payload?", pool=pool, rows=copy.deepcopy([row]),
        evidence=copy.deepcopy(evidence),
    ) == baseline
