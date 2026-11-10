"""Persist the map units required to resolve and filter native search hits."""

from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.lexical.input import LexicalInput
from shared.services.retrieval.scoring.knowhere_hybrid import MAP_UNIT_INDEX_FORMAT_VERSION


def persist_unit_metadata(
    db: Session, *, storage: CorpusStorage, document_id: str, revision_id: str,
    payload: LexicalInput, replace_existing: bool = False,
) -> None:
    """Maintain the unit catalog without writing PostgreSQL scoring terms."""
    if replace_existing:
        db.execute(delete(storage.DocumentMapUnitIndex).where(
            storage.DocumentMapUnitIndex.document_id == document_id,
            storage.DocumentMapUnitIndex.job_result_id == revision_id,
        ))
        db.execute(delete(storage.DocumentMapUnit).where(
            storage.DocumentMapUnit.document_id == document_id,
            storage.DocumentMapUnit.job_result_id == revision_id,
        ))
    rows: list[dict[str, object]] = []
    for unit in payload.units:
        identity: str = sha256(json.dumps([revision_id, unit.unitId]).encode()).hexdigest()
        rows.append({"id": f"dmu_{identity}", "document_id": document_id,
            "job_result_id": revision_id, "unit_id": unit.unitId, "section_id": unit.sectionId,
            "unit_kind": "self_only" if unit.unitId.endswith("__self") else "leaf",
            "path_token_count": len(unit.pathTokens.split()), "content_token_count": len(unit.contentTokens.split()),
            "has_image": unit.hasImage, "has_table": unit.hasTable, "sort_order": unit.sortOrder})
    for offset in range(0, len(rows), 1000):
        db.execute(insert(storage.DocumentMapUnit).values(rows[offset:offset + 1000]).on_conflict_do_nothing())
    db.execute(insert(storage.DocumentMapUnitIndex).values(
        id=f"dmui_{uuid4().hex}", document_id=document_id, job_result_id=revision_id,
        format_version=MAP_UNIT_INDEX_FORMAT_VERSION, unit_count=len(payload.units), token_count=0,
        average_idf_path=0, average_idf_content=0,
        path_document_count=sum(bool(unit.pathTokens) for unit in payload.units),
        content_document_count=sum(bool(unit.contentTokens) for unit in payload.units),
        path_total_length=sum(len(unit.pathTokens.split()) for unit in payload.units),
        content_total_length=sum(len(unit.contentTokens.split()) for unit in payload.units),
    ).on_conflict_do_nothing(index_elements=["document_id", "job_result_id"]))
