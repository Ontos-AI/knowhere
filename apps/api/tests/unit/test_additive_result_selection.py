"""Protect coarse chapter coverage while enrichment publishes."""

import pytest

from shared.services.retrieval.hydration.result_assembly import assemble_retrieval_results
from shared.services.retrieval.search.ranking import rank_candidates_by_path


@pytest.mark.asyncio
async def test_same_path_partial_attempt_preserves_baseline_until_all_pages_are_replaced() -> None:
    baseline = {"document_id": "document", "job_result_id": "revision", "build_id": "first",
        "section_path": "Chapter", "chunk_type": "page", "chunk_id": "coarse", "source_row_id": "coarse-row",
        "uses_additive_visibility": True, "content": "Original text across both pages",
        "chunk_metadata": {"scope_id": "p5-6", "page_nums": [5, 6], "enrichment_state": "coarse"}}
    fine = {**baseline, "build_id": "attempt", "chunk_id": "fine", "source_row_id": "fine-row",
        "enrichment_attempt_id": "attempt", "enrichment_scope_id": "p5-6",
        "content": "Source content from both pages", "enrichment_publication_state": "writing",
        "chunk_metadata": {"page_nums": [5, 6], "searchable_page_nums": [5, 6]}}

    rows = rank_candidates_by_path([baseline, fine], [], 2)
    assert [row["chunk_id"] for row in rows] == ["coarse", "fine"]
    result = await assemble_retrieval_results(rows=rows, exclude_document_ids=[], exclude_sections=[])
    assert [row["chunk_id"] for row in result] == ["coarse", "fine"]

    baseline["matched_page_nums"] = [5]
    fine["enrichment_publication_state"] = "published"
    fine["chunk_metadata"] = {"page_nums": [5, 6], "searchable_page_nums": [5]}
    result = await assemble_retrieval_results(rows=[baseline, fine], exclude_document_ids=[], exclude_sections=[])
    assert "coarse" in {row["chunk_id"] for row in result}

    fine["chunk_metadata"]["searchable_page_nums"] = [5, 6]
    result = await assemble_retrieval_results(rows=[baseline, fine], exclude_document_ids=[], exclude_sections=[])
    assert [row["chunk_id"] for row in result] == ["fine"]
