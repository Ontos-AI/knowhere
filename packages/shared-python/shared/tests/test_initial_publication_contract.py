import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def configure_import_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = {
        "S3_BUCKET_NAME": "unit-test",
        "S3_TEMP_PATH": "/tmp",
        "TMP_PATH": "/tmp",
        "DATABASE_URL": "postgresql+asyncpg://unit@127.0.0.1:1/unit",
    }
    for name, value in defaults.items():
        if name not in os.environ:
            monkeypatch.setenv(name, value)


def test_directory_accepts_a_newer_committed_version() -> None:
    from shared.models.schemas.document_directory import DocumentDirectoryResponse

    directory = DocumentDirectoryResponse(
        document_id="doc_123",
        job_id="job_123",
        job_result_id="result_123",
        namespace="library",
        directory_version=2,
        directory_committed_at="2026-11-20T00:00:00Z",
    )

    assert directory.directory_version == 2


def test_private_native_publication_uses_additive_contract_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.core.config import settings
    from shared.services.retrieval.corpus_storage import CorpusStorage, DEMO_NAMESPACE

    monkeypatch.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "opensearch")
    assert CorpusStorage.resolve_namespace("library").usesAdditivePublication()
    assert not CorpusStorage.resolve_namespace(DEMO_NAMESPACE).usesAdditivePublication()

    monkeypatch.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "postgresql")
    assert not CorpusStorage.resolve_namespace("library").usesAdditivePublication()


def test_new_document_replacement_cannot_also_name_an_update_target() -> None:
    from shared.models.schemas.job import JobCreateV2

    with pytest.raises(ValidationError, match="replaces_document_id"):
        JobCreateV2.model_validate({
            "source_type": "file",
            "file_name": "source.pdf",
            "document_id": "doc_current",
            "replaces_document_id": "doc_current",
        })


def test_enrichment_failure_events_deduplicate_by_attempt() -> None:
    from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox

    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(
        webhook_url="https://subscriber.example/notify",
        webhook_enabled=True,
        webhook_events=["job.enrichment.failed"],
        failure_stage=None,
        parsing_status="completed",
        indexing_status="completed",
        current_indexing_attempt_id=None,
    )
    db.execute.return_value.one_or_none.return_value = SimpleNamespace(id="result_123")
    persisted = {}
    db.add.side_effect = lambda event: persisted.setdefault(event.milestone_key, event)
    db.scalars.side_effect = lambda statement: SimpleNamespace(
        first=lambda: next(
            (persisted[value] for value in statement.compile().params.values()
             if isinstance(value, str) and value in persisted),
            None,
        )
    )
    outbox = SyncJobWebhookOutbox()

    def create(attempt_key: str) -> str:
        event = outbox.create_event(
            db,
            job_id="job_123",
            event_type="job.enrichment.failed",
            attempt_key=attempt_key,
            extra_payload={"unit_id": "p1-4", "attempt": 1},
        )
        assert event is not None
        return event.event_id

    first = create("attempt-1")
    second = create("attempt-2")
    assert first != second
    assert create("attempt-1") == first
    assert len(persisted) == 2


@pytest.mark.parametrize(("subscribed_events", "event_type", "delivers_event"), [
    (None, "job.enrichment.failed", False),
    (["job.completed"], "job.enrichment.failed", False),
    (None, "job.completed", True),
    (None, "job.failed", True),
    (None, "job.document.directory_ready", True),
])
def test_hipo_callback_respects_ordinary_subscriptions(
    monkeypatch: pytest.MonkeyPatch,
    subscribed_events: list[str] | None,
    event_type: str,
    delivers_event: bool,
) -> None:
    from shared.core.config import settings
    from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox

    # Simulate a deployment that still has the retired setting.
    monkeypatch.setitem(vars(settings), "KNOWHERE_HIPO_CALLBACK_ORIGIN", "https://hipo.example")
    is_initial_failure: bool = event_type == "job.failed"
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(
        webhook_url="https://hipo.example/api/internal/webhooks/knowhere/parse",
        webhook_enabled=True, webhook_events=subscribed_events,
        failure_stage="parsing" if is_initial_failure else None,
        parsing_status="failed" if is_initial_failure else "completed",
        indexing_status="pending" if is_initial_failure else "completed",
        current_indexing_attempt_id=None,
        error_message="Cannot parse the document" if is_initial_failure else None,
        error_code="INTERNAL_ERROR" if is_initial_failure else None,
        job_metadata={},
    )
    db.scalars.return_value.first.return_value = None
    db.execute.return_value.one_or_none.return_value = (
        None if is_initial_failure else SimpleNamespace(id="result_123", document_metadata={})
    )
    event = SyncJobWebhookOutbox().create_event(
        db, job_id="job_123", event_type=event_type,
        attempt_key="attempt-1" if event_type == "job.enrichment.failed" else None,
    )

    assert (event is not None) == delivers_event
