from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.chunks.same_as_markers import (  # noqa: E402
    contains_same_as_marker,
    strip_same_as_markers,
)
from shared.services.retrieval.hydration.result_assembly import (  # noqa: E402
    assemble_retrieval_results,
)
from shared.services.retrieval.hydration.same_as import (  # noqa: E402
    resolve_navigation_summary,
    resolve_page_evidence,
)
from shared.services.storage.zip_doc_navigation import ZipDocNavigationBuilder  # noqa: E402


def _page_row(
    *,
    chunk_id: str,
    path: str,
    summary: str,
    page_nums: list[int],
    owned_page_nums: list[int],
    connect_to: list[dict] | None = None,
    entities: list[dict] | None = None,
    document_id: str = "doc-1",
    job_result_id: str = "jr-1",
    content: str = "",
) -> dict:
    return {
        "chunk_id": chunk_id,
        "chunk_type": "page",
        "document_id": document_id,
        "job_result_id": job_result_id,
        "section_path": path,
        "source_file_name": "demo.pdf",
        "content": content,
        "score": 1.0,
        "chunk_metadata": {
            "summary": summary,
            "entities": entities or [],
            "keywords": [item["text"] for item in (entities or [])],
            "page_nums": page_nums,
            "owned_page_nums": owned_page_nums,
            "connect_to": connect_to or [],
            "content_kind": "body",
        },
    }


def test_strip_same_as_markers_centralized() -> None:
    text = "lead [SAME-AS demo.pdf/Owner p2] trail"
    cleaned = strip_same_as_markers(text)
    assert "SAME-AS" not in cleaned
    assert cleaned.startswith("lead")
    assert cleaned.endswith("trail")
    assert contains_same_as_marker(text)
    assert not contains_same_as_marker(cleaned)


def test_resolve_pure_alias_uses_owner_summary() -> None:
    owner = _page_row(
        chunk_id="owner",
        path="demo.pdf/Owner",
        summary="owned summary",
        page_nums=[2],
        owned_page_nums=[2],
        entities=[{"text": "Acme", "type": "organization"}],
    )
    alias = _page_row(
        chunk_id="alias",
        path="demo.pdf/Alias",
        summary="",
        page_nums=[2],
        owned_page_nums=[],
        content="[SAME-AS demo.pdf/Owner p2]",
        connect_to=[
            {
                "target": "owner",
                "relation": "same_as",
                "ref": "[SAME-AS demo.pdf/Owner p2]",
                "page": 2,
            }
        ],
    )

    resolution = resolve_page_evidence(
        alias,
        rows_by_chunk_id={"owner": owner, "alias": alias},
    )

    assert resolution.content_source == "same_as_owner_summary"
    assert resolution.summary == "owned summary"
    assert resolution.entities == [{"text": "Acme", "type": "organization"}]
    assert resolution.content_chunk_ids == ["owner"]
    assert resolution.matched_chunk_id == "alias"


def test_resolve_mixed_leaf_merges_own_and_owner_semantics() -> None:
    owner = _page_row(
        chunk_id="owner",
        path="demo.pdf/Owner",
        summary="page-1 owner summary",
        page_nums=[1],
        owned_page_nums=[1],
        entities=[{"text": "OwnerOrg", "type": "organization"}],
    )
    mixed = _page_row(
        chunk_id="mixed",
        path="demo.pdf/Mixed",
        summary="page-2 own summary",
        page_nums=[1, 2],
        owned_page_nums=[2],
        entities=[{"text": "OwnOrg", "type": "organization"}],
        connect_to=[
            {
                "target": "owner",
                "relation": "same_as",
                "ref": "[SAME-AS demo.pdf/Owner p1]",
                "page": 1,
            }
        ],
    )

    resolution = resolve_page_evidence(
        mixed,
        rows_by_chunk_id={"owner": owner, "mixed": mixed},
    )

    assert resolution.content_source == "mixed_page_summary"
    assert "page-2 own summary" in resolution.summary
    assert "Page 1: page-1 owner summary" in resolution.summary
    assert {entity["text"] for entity in resolution.entities} == {"OwnOrg", "OwnerOrg"}
    assert resolution.content_chunk_ids == ["mixed", "owner"]


def test_resolve_ignores_self_loop_and_missing_owner() -> None:
    alias = _page_row(
        chunk_id="alias",
        path="demo.pdf/Alias",
        summary="",
        page_nums=[3],
        owned_page_nums=[],
        connect_to=[
            {
                "target": "alias",
                "relation": "same_as",
                "ref": "[SAME-AS demo.pdf/Alias p3]",
                "page": 3,
            },
            {
                "target": "missing",
                "relation": "same_as",
                "ref": "[SAME-AS demo.pdf/Missing p3]",
                "page": 3,
            },
        ],
    )

    resolution = resolve_page_evidence(alias, rows_by_chunk_id={"alias": alias})

    assert resolution.summary == ""
    assert resolution.content_source == "summary"
    assert resolution.content_chunk_ids == ["alias"]


def test_resolve_rejects_cross_revision_owner() -> None:
    owner = _page_row(
        chunk_id="owner",
        path="demo.pdf/Owner",
        summary="should not leak",
        page_nums=[1],
        owned_page_nums=[1],
        job_result_id="jr-other",
    )
    alias = _page_row(
        chunk_id="alias",
        path="demo.pdf/Alias",
        summary="",
        page_nums=[1],
        owned_page_nums=[],
        connect_to=[
            {
                "target": "owner",
                "relation": "same_as",
                "ref": "[SAME-AS demo.pdf/Owner p1]",
                "page": 1,
            }
        ],
    )

    resolution = resolve_page_evidence(
        alias,
        rows_by_chunk_id={"owner": owner, "alias": alias},
    )
    assert resolution.summary == ""
    assert resolution.content_source == "summary"


@pytest.mark.asyncio
async def test_classic_assembly_alias_uses_owner_evidence_keeps_alias_citation() -> None:
    owner = _page_row(
        chunk_id="owner",
        path="demo.pdf/Owner",
        summary="owner body summary",
        page_nums=[2],
        owned_page_nums=[2],
        entities=[{"text": "Acme", "type": "organization"}],
    )
    alias = _page_row(
        chunk_id="alias",
        path="demo.pdf/Alias",
        summary="",
        page_nums=[2],
        owned_page_nums=[],
        content="[SAME-AS demo.pdf/Owner p2]",
        connect_to=[
            {
                "target": "owner",
                "relation": "same_as",
                "ref": "[SAME-AS demo.pdf/Owner p2]",
                "page": 2,
            },
            {
                "target": "table-1",
                "relation": "related",
                "ref": "[tables/t.html]",
                "page": 2,
            },
        ],
    )
    table = {
        "chunk_id": "table-1",
        "chunk_type": "table",
        "document_id": "doc-1",
        "job_result_id": "jr-1",
        "section_path": "tables/t.html",
        "file_path": "tables/t.html",
        "content": "<table></table>",
        "chunk_metadata": {"summary": "table summary", "keywords": ["col"]},
        "sort_order": 1,
    }

    # Alias hit only — owner is available via in-memory hydrate fallback path
    # when already present in rows; here include table + alias, owner via same rows.
    assembled = await assemble_retrieval_results(
        rows=[alias, owner, table],
        exclude_document_ids=[],
        exclude_sections=[],
    )

    by_id = {row["chunk_id"]: row for row in assembled}
    assert "alias" in by_id
    assert "owner" in by_id  # independently present in filtered hits
    assert "table-1" not in by_id  # media suppressed as embedded target
    assert by_id["alias"]["section_path"] == "demo.pdf/Alias"
    assert by_id["alias"]["content_source"] == "same_as_owner_summary"
    assert "owner body summary" in by_id["alias"]["content"]
    assert "table summary" in by_id["alias"]["content"]
    assert "SAME-AS" not in by_id["alias"]["content"]
    assert "_content_chunk_ids" not in by_id["alias"]


@pytest.mark.asyncio
async def test_classic_assembly_alias_only_hit_hydrates_owner_evidence(
    monkeypatch,
) -> None:
    owner = _page_row(
        chunk_id="owner",
        path="demo.pdf/Owner",
        summary="owner body summary",
        page_nums=[2],
        owned_page_nums=[2],
        entities=[{"text": "Acme", "type": "organization"}],
    )
    alias = _page_row(
        chunk_id="alias",
        path="demo.pdf/Alias",
        summary="",
        page_nums=[2],
        owned_page_nums=[],
        connect_to=[
            {
                "target": "owner",
                "relation": "same_as",
                "ref": "[SAME-AS demo.pdf/Owner p2]",
                "page": 2,
            }
        ],
    )

    async def _fake_hydrate(**kwargs):
        relations = set(kwargs.get("relations") or [])
        if relations == {"same_as"}:
            return [owner]
        return []

    monkeypatch.setattr(
        "shared.services.retrieval.hydration.result_assembly.hydrate_connected_target_rows",
        _fake_hydrate,
    )

    assembled = await assemble_retrieval_results(
        rows=[alias],
        exclude_document_ids=[],
        exclude_sections=[],
    )

    assert [row["chunk_id"] for row in assembled] == ["alias"]
    assert assembled[0]["section_path"] == "demo.pdf/Alias"
    assert assembled[0]["content"] == "owner body summary"
    assert assembled[0]["content_source"] == "same_as_owner_summary"
    assert assembled[0]["chunk_metadata"]["entities"] == [
        {"text": "Acme", "type": "organization"}
    ]


@pytest.mark.asyncio
async def test_agentic_selection_mounts_media_under_alias_path(monkeypatch) -> None:
    from shared.services.retrieval.agentic.navigation import selection_hydration

    alias = _page_row(
        chunk_id="alias",
        path="demo.pdf/Alias",
        summary="",
        page_nums=[2],
        owned_page_nums=[],
        connect_to=[
            {
                "target": "owner",
                "relation": "same_as",
                "ref": "[SAME-AS demo.pdf/Owner p2]",
                "page": 2,
            },
            {
                "target": "table-1",
                "relation": "related",
                "ref": "[tables/t.html]",
                "page": 2,
            },
        ],
    )
    owner = _page_row(
        chunk_id="owner",
        path="demo.pdf/Owner",
        summary="owner summary",
        page_nums=[2],
        owned_page_nums=[2],
    )
    table = {
        "chunk_id": "table-1",
        "chunk_type": "table",
        "document_id": "doc-1",
        "job_result_id": "jr-1",
        "section_path": "tables/t.html",
        "file_path": "tables/t.html",
        "content": "tables/t.html",
        "chunk_metadata": {"summary": "table summary"},
    }

    async def _fake_hydrate(**kwargs):
        relations = set(kwargs.get("relations") or [])
        if relations == {"same_as"}:
            return [owner]
        if relations == {"embeds", "related"} or relations == {"related", "embeds"}:
            return [table]
        return []

    monkeypatch.setattr(
        selection_hydration,
        "hydrate_connected_target_rows",
        _fake_hydrate,
    )

    class _FakeDB:
        pass

    chunks = await selection_hydration._materialize_same_as_and_append_media(
        _FakeDB(),
        [alias],
    )

    page = next(chunk for chunk in chunks if chunk["chunk_id"] == "alias")
    assert page["chunk_metadata"]["summary"] == "owner summary"
    assert page["content_source"] == "same_as_owner_summary"
    assert all(chunk["chunk_id"] != "owner" for chunk in chunks)

    media = [chunk for chunk in chunks if chunk["chunk_id"] == "table-1"]
    assert len(media) == 1
    assert media[0]["owner_section_path"] == "demo.pdf/Alias"
    chunks = [
        {
            "chunk_id": "owner",
            "type": "page",
            "path": "demo.pdf/Owner",
            "content": "owned body",
            "metadata": {
                "summary": "owner nav summary",
                "page_nums": [2],
                "owned_page_nums": [2],
                "connect_to": [],
            },
        },
        {
            "chunk_id": "alias",
            "type": "page",
            "path": "demo.pdf/Alias",
            "content": "[SAME-AS demo.pdf/Owner p2]",
            "metadata": {
                "summary": "",
                "page_nums": [2],
                "owned_page_nums": [],
                "connect_to": [
                    {
                        "target": "owner",
                        "relation": "same_as",
                        "ref": "[SAME-AS demo.pdf/Owner p2]",
                        "page": 2,
                    }
                ],
            },
        },
    ]

    assert resolve_navigation_summary(
        chunks[1],
        chunks_by_id={chunk["chunk_id"]: chunk for chunk in chunks},
    ) == "owner nav summary"

    nav = ZipDocNavigationBuilder().build_doc_nav(chunks, "demo.pdf")
    alias_section = next(
        section
        for section in nav["sections"]
        if section["path"].endswith("Alias")
    )
    assert alias_section["summary"] == "owner nav summary"
    assert "SAME-AS" not in alias_section["summary"]
