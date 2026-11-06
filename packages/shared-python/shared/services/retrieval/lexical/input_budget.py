"""SQL-only logical byte and conservative working-set bounds before hydration."""

from typing import Literal

from sqlalchemy import BigInteger, JSON, String, Table, Text, cast, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from shared.core.config import settings
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.lexical.error import LexicalIndexError

_Purpose = Literal["raw", "publication", "manifest", "section_ids"]
_TEXT_WORKING_MULTIPLIER: int = 4
_JSON_WORKING_MULTIPLIER: int = 64
_ROW_WORKING_BYTES: int = 4_096
_PROJECTED_SECTION_WORKING_BYTES: int = 512
_WORKING_LIMIT_MULTIPLIER: int = 4
_SECTION_PUBLICATION_FIELDS: frozenset[str] = frozenset((
    "section_id", "parent_section_id", "section_path", "section_title", "summary",
))
_CHUNK_PUBLICATION_FIELDS: frozenset[str] = frozenset(("chunk_id", "section_id", "chunk_type", "chunk_metadata"))


def _measureTable(db: Session, table: Table, *, documentId: str, revisionId: str,
                  isSection: bool, purpose: _Purpose) -> tuple[int, int]:
    rawBytes: ColumnElement[int] = cast(func.coalesce(0, 0), BigInteger)
    workingBytes: ColumnElement[int] = cast(
        _PROJECTED_SECTION_WORKING_BYTES if purpose == "section_ids" else _ROW_WORKING_BYTES, BigInteger)
    for column in table.columns:
        if purpose == "section_ids" and column.name != "section_id":
            continue
        if purpose in ("publication", "manifest") and column.name not in (
            _SECTION_PUBLICATION_FIELDS if isSection else _CHUNK_PUBLICATION_FIELDS):
            continue
        isJson: bool = isinstance(column.type, JSON)
        if not isJson and not isinstance(column.type, String):
            continue
        value: ColumnElement = column
        if isJson:
            if purpose == "manifest" and not isSection:
                value = column["connect_to"]
            value = cast(value, Text)
        length: ColumnElement[int] = cast(func.coalesce(func.octet_length(value), 0), BigInteger)
        rawBytes = rawBytes + length
        workingBytes = workingBytes + length * (_JSON_WORKING_MULTIPLIER if isJson else _TEXT_WORKING_MULTIPLIER)
    logical, working = db.execute(select(func.coalesce(func.sum(rawBytes), 0),
        func.coalesce(func.sum(workingBytes), 0)).where(table.c.document_id == documentId,
            table.c.job_result_id == revisionId)).one()
    return int(logical), int(working)


def _assertBudget(db: Session, *, storage: CorpusStorage, documentId: str,
                  revisionId: str, purpose: _Purpose) -> int:
    # The existing cap still bounds logical decompressed input bytes. Its 4x
    # working-set allowance covers row objects, Python strings/JSON containers
    # and the shallow metadata copy; this is an estimate, not an RSS promise.
    maximumBytes: int = settings.KNOWHERE_INDEXING_INPUT_MAXIMUM_BYTES
    workingLimit: int = maximumBytes * _WORKING_LIMIT_MULTIPLIER
    logical: int = 0
    working: int = 0
    tables: tuple[tuple[Table, bool], ...] = ((storage.DocumentSection.__table__, True),)
    if purpose != "section_ids":
        tables += ((storage.DocumentChunk.__table__, False),)
    for table, isSection in tables:
        rawBytes, workingBytes = _measureTable(db, table, documentId=documentId,
            revisionId=revisionId, isSection=isSection, purpose=purpose)
        logical += rawBytes
        working += workingBytes
        if logical > maximumBytes or working > workingLimit:
            raise LexicalIndexError(f"Indexing input hydration budget exceeded: logical_bytes={logical}, "
                f"logical_limit={maximumBytes}, estimated_working_bytes={working}, working_limit={workingLimit}", isSizeLimit=True)
    return working


class LexicalInputBudget:
    @staticmethod
    def assertCanHydrate(db: Session, *, storage: CorpusStorage, documentId: str,
                         revisionId: str, areOnlySectionIdsNeeded: bool = False) -> int:
        """Call under the existing indexing SQL deadline before selecting raw rows."""
        return _assertBudget(db, storage=storage, documentId=documentId, revisionId=revisionId,
            purpose="section_ids" if areOnlySectionIdsNeeded else "raw")

    @staticmethod
    def assertCanPublish(db: Session, *, storage: CorpusStorage, documentId: str,
                         revisionId: str, doesIncludeGraphMetadata: bool = True) -> int:
        """Bound only the fields actually read by serving manifests and graphs."""
        return _assertBudget(db, storage=storage, documentId=documentId, revisionId=revisionId,
            purpose="publication" if doesIncludeGraphMetadata else "manifest")
