"""Maintain the temporary PG rollback copy independently of search readiness."""

from collections import Counter
from hashlib import sha256

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import app_config
from shared.core.database_sync import get_sync_session_factory
from shared.models.database.job_result import JobResult
from shared.models.database.job import Job
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.demo_authorization import authorize_demo_transaction
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.input import LexicalInput
from shared.services.retrieval.lexical.input_retention import LexicalInputRetention
from shared.services.retrieval.lexical.storage import LexicalInputStorage
from shared.services.retrieval.publication_token_copy import TokenCopyRow, insert_token_records_with_copy
from shared.services.retrieval.scoring.persisted_score_load import average_idf_from_unit_dfs
from shared.services.storage.storage_adapter import StorageAdapter


class LegacyLexicalCopy:
    def __init__(self, *, sessionFactory: sessionmaker[Session] | None = None) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory or get_sync_session_factory()

    def run(self, buildId: str) -> bool:
        with self.sessions.begin() as db:
            build: LexicalBuild = db.scalars(select(LexicalBuild).where(LexicalBuild.id == buildId)).one()
            if build.legacyCopyState in ("completed", "skipped"):
                return True
            if build.legacyCopyState != "pending":
                return False
            registry: LexicalNamespaceIndex = db.scalars(select(LexicalNamespaceIndex).where(
                LexicalNamespaceIndex.id == build.namespaceIndexId)).one()
            storage: CorpusStorage = CorpusStorage.resolve_namespace(registry.namespace)
            result: JobResult = db.scalars(select(JobResult).where(JobResult.id == build.revisionId).with_for_update(of=JobResult)).one()
            actorId: str = db.scalars(select(Job.user_id).where(Job.job_id == result.job_id)).one()
            revisionId: str = build.revisionId
            documentId: str = build.documentId
            if not self._isRequired(db, build, storage):
                build.legacyCopyState = "skipped"
                return True
            if not result.indexing_input_key or not result.indexing_input_checksum:
                raise LexicalIndexError("PG rollback copy has no frozen lexical input")
            LexicalInputRetention.assertRetained(db, result)
            key: str = result.indexing_input_key
            checksum: str = result.indexing_input_checksum
        # Optional copies may run on different retired/recreated gevent hubs.
        # Close each private transport on the hub that used it, before COPY.
        adapter: StorageAdapter = app_config.get_storage_adapter(doesOwnTransport=True)
        try:
            payload: LexicalInput = LexicalInputStorage().load(key=key, checksum=checksum, adapter=adapter)
        finally:
            adapter.close()
        if any(unit.documentId != documentId or unit.revisionId != revisionId for unit in payload.units):
            raise LexicalIndexError("PG rollback input escaped its published revision")
        with self.sessions.begin() as db:
            if storage.is_demo:
                authorize_demo_transaction(db, user_id=actorId)
            unitIds: dict[str, str] = dict(db.execute(select(
                storage.DocumentMapUnit.unit_id, storage.DocumentMapUnit.id).where(
                storage.DocumentMapUnit.document_id == documentId,
                storage.DocumentMapUnit.job_result_id == revisionId)).tuples().all())
        if set(unitIds) != {unit.unitId for unit in payload.units}:
            raise LexicalIndexError("PG rollback units differ from the published lexical input")
        rows, pathFrequency, contentFrequency = self._prepareRows(payload, unitIds, revisionId)
        with self.sessions.begin() as db:
            if storage.is_demo:
                authorize_demo_transaction(db, user_id=actorId)
            # Copies share only this revision lock. Neither generation/control
            # nor the old build row is locked during COPY, so new publication
            # can retire that build immediately. Repair also works while the
            # primary backend's publication gate is closed for rollback.
            lockIdentity: int = int.from_bytes(sha256(f"legacy-copy:{revisionId}".encode()).digest()[:8], "big", signed=True)
            if not db.scalar(select(func.pg_try_advisory_xact_lock(lockIdentity))):
                return False
            build = db.scalars(select(LexicalBuild).where(LexicalBuild.id == buildId)).one()
            if build.legacyCopyState != "pending":
                return build.legacyCopyState in ("completed", "skipped")
            if not self._isRequired(db, build, storage):
                build.legacyCopyState = "skipped"
                return True
            db.execute(delete(storage.DocumentMapUnitToken).where(
                storage.DocumentMapUnitToken.map_unit_id.in_(list(unitIds.values()))))
            insert_token_records_with_copy(db, rows, corpus_storage=storage)
            marker = db.scalars(select(storage.DocumentMapUnitIndex).where(
                storage.DocumentMapUnitIndex.document_id == documentId,
                storage.DocumentMapUnitIndex.job_result_id == revisionId)).one()
            marker.token_count = len(rows)
            marker.average_idf_path = average_idf_from_unit_dfs(unit_count=len(unitIds), token_document_frequency=pathFrequency)
            marker.average_idf_content = average_idf_from_unit_dfs(unit_count=len(unitIds), token_document_frequency=contentFrequency)
        # The guarded completion changes only this build's copy status, never
        # a newer revision's active pointer, generation or completion history.
        with self.sessions.begin() as db:
            build = db.scalars(select(LexicalBuild).where(LexicalBuild.id == buildId).with_for_update()).one()
            if build.legacyCopyState == "pending":
                build.legacyCopyState = "completed"
        return True

    @staticmethod
    def _isRequired(db: Session, build: LexicalBuild, storage: CorpusStorage) -> bool:
        currentRevision: str | None = db.scalar(select(storage.Document.current_job_result_id).where(
            storage.Document.document_id == build.documentId,
            storage.Document.status == "active"))
        return build.state == "published" and currentRevision == build.revisionId

    @staticmethod
    def _prepareRows(payload: LexicalInput, unitIds: dict[str, str], revisionId: str
                     ) -> tuple[list[TokenCopyRow], Counter[str], Counter[str]]:
        rows: list[TokenCopyRow] = []
        pathFrequency: Counter[str] = Counter()
        contentFrequency: Counter[str] = Counter()
        for unit in payload.units:
            for channel, tokens, frequency in (("path", unit.pathTokens, pathFrequency),
                ("content", unit.contentTokens, contentFrequency)):
                counts: Counter[str] = Counter(tokens.split())
                frequency.update(counts.keys())
                for token, count in counts.items():
                    rows.append(["", unitIds[unit.unitId], channel, token,
                                 sha256(token.encode()).hexdigest(), count])
        rows.sort(key=lambda row: (str(row[2]), str(row[4]), str(row[1]), str(row[3])))
        prefix: str = sha256(revisionId.encode()).hexdigest()[:12]
        for position, row in enumerate(rows):
            row[0] = f"dmut_{prefix}{position:019d}"
        return rows, pathFrequency, contentFrequency
