"""Service-entry tests for same-name document conflict on create (no document_id)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from tests.support.import_environment import (
    configure_import_environment,
    ensure_import_paths,
)

configure_import_environment()
ensure_import_paths()

from app.services.document_ingestion.creation_service import (  # noqa: E402
    DocumentIngestionCreationService,
    ResolvedDocumentIngestionScope,
)
from app.services.document_ingestion.scope_service import (  # noqa: E402
    find_active_document_by_source_file_name,
)
from app.services.rate_limit.data_structures import CurrentUser  # noqa: E402
from shared.core.exceptions.domain_exceptions import ConflictException  # noqa: E402
from shared.models.database.document import Document  # noqa: E402
from shared.models.schemas.job import JobCreate  # noqa: E402


USER_ID = "user_dup"
NAMESPACE = "default"
FILE_NAME = "01-p1-70.pdf"
EXISTING_DOC = "doc_existing12"
OLDER_DOC = "doc_older00001"
NEWER_DOC = "doc_newer00002"


class _AsyncSessionAdapter:
    def __init__(self, session: Session) -> None:
        self._session = session

    async def execute(self, statement):  # noqa: ANN001
        return self._session.execute(statement)


def _seed_document(
    session: Session,
    *,
    document_id: str,
    source_file_name: str,
    namespace: str = NAMESPACE,
    status: str = "active",
    user_id: str = USER_ID,
    created_at: datetime | None = None,
) -> None:
    now = created_at or datetime(2026, 1, 1)
    session.add(
        Document(
            document_id=document_id,
            user_id=user_id,
            namespace=namespace,
            status=status,
            source_file_name=source_file_name,
            parse_track="chunk",
            created_at=now,
            updated_at=now,
        )
    )
    session.commit()


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _disable_fk(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")

    Document.__table__.create(engine)
    session = Session(engine)
    _seed_document(session, document_id=EXISTING_DOC, source_file_name=FILE_NAME)
    return session


def _file_payload(*, document_id: str | None = None) -> JobCreate:
    payload: dict[str, str] = {
        "source_type": "file",
        "file_name": FILE_NAME,
        "namespace": NAMESPACE,
    }
    if document_id:
        payload["document_id"] = document_id
    return JobCreate.model_validate(payload)


def _scope() -> ResolvedDocumentIngestionScope:
    return ResolvedDocumentIngestionScope(
        job_metadata={},
        document_id="doc_newscope01",
        namespace=NAMESPACE,
    )


async def _reject(
    db_session: Session,
    *,
    payload: JobCreate,
) -> None:
    await DocumentIngestionCreationService()._reject_duplicate_source_file_name(
        _AsyncSessionAdapter(db_session),  # type: ignore[arg-type]
        payload=payload,
        current_user=CurrentUser(user_id=USER_ID, user_tier="pro"),
        scope=_scope(),
        source_file_name=FILE_NAME,
    )


@pytest.mark.asyncio
async def test_find_returns_oldest_active_document_for_exact_name(
    db_session: Session,
) -> None:
    _seed_document(
        db_session,
        document_id=NEWER_DOC,
        source_file_name=FILE_NAME,
        created_at=datetime(2026, 3, 1),
    )
    _seed_document(
        db_session,
        document_id=OLDER_DOC,
        source_file_name=FILE_NAME,
        created_at=datetime(2025, 12, 1),
    )
    found = await find_active_document_by_source_file_name(
        _AsyncSessionAdapter(db_session),  # type: ignore[arg-type]
        user_id=USER_ID,
        namespace=NAMESPACE,
        source_file_name=FILE_NAME,
    )
    assert found is not None
    assert found.document_id == OLDER_DOC


@pytest.mark.asyncio
async def test_find_ignores_other_user_other_namespace_archived_and_case(
    db_session: Session,
) -> None:
    adapter = _AsyncSessionAdapter(db_session)
    other_user = await find_active_document_by_source_file_name(
        adapter,  # type: ignore[arg-type]
        user_id="someone_else",
        namespace=NAMESPACE,
        source_file_name=FILE_NAME,
    )
    other_ns = await find_active_document_by_source_file_name(
        adapter,  # type: ignore[arg-type]
        user_id=USER_ID,
        namespace="other",
        source_file_name=FILE_NAME,
    )
    different_case = await find_active_document_by_source_file_name(
        adapter,  # type: ignore[arg-type]
        user_id=USER_ID,
        namespace=NAMESPACE,
        source_file_name="01-P1-70.PDF",
    )
    db_session.query(Document).filter(Document.document_id == EXISTING_DOC).update(
        {"status": "archived"}
    )
    db_session.commit()
    archived = await find_active_document_by_source_file_name(
        adapter,  # type: ignore[arg-type]
        user_id=USER_ID,
        namespace=NAMESPACE,
        source_file_name=FILE_NAME,
    )
    assert other_user is None
    assert other_ns is None
    assert different_case is None
    assert archived is None


@pytest.mark.asyncio
async def test_reject_duplicate_file_name_without_document_id(
    db_session: Session,
) -> None:
    with pytest.raises(ConflictException) as caught:
        await _reject(db_session, payload=_file_payload())
    error = caught.value
    assert error.details == {
        "reason": "ALREADY_EXISTS",
        "resource": "Document",
        "id": EXISTING_DOC,
    }
    assert error.user_message == (
        f"A document named {FILE_NAME!r} already exists. "
        "To replace it, retry with document_id set to details.id."
    )


@pytest.mark.asyncio
async def test_reject_skips_when_document_id_is_provided(
    db_session: Session,
) -> None:
    await _reject(db_session, payload=_file_payload(document_id=EXISTING_DOC))
