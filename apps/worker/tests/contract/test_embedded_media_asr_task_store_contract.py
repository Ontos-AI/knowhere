"""Each embedded recording keeps its own resumable ASR task on the job row."""

from __future__ import annotations


def _seed_job() -> None:
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.job import Job
    from shared.models.database.user import User

    with get_sync_db_context() as session:
        session.add(User(id="asr-owner", email="asr-owner@example.invalid", name="ASR owner"))
        session.flush()
        session.add(
            Job(
                job_id="deck-asr",
                user_id="asr-owner",
                job_type="document_ingestion",
                source_type="file",
                status="running",
                job_metadata={"document_id": "doc_deck", "media_asr_task": {"task_id": "t0", "token_id": "k0"}},
            )
        )


def test_recordings_resume_their_own_tasks(worker_contract_environment: None) -> None:
    from app.services.document_parser.formats.media.asr_task_store import (
        AsrTaskRef,
        EmbeddedMediaAsrTaskStore,
        JobMetadataAsrTaskStore,
    )
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.job import Job

    _seed_job()
    first = EmbeddedMediaAsrTaskStore("a" * 64)
    second = EmbeddedMediaAsrTaskStore("b" * 64)
    assert first.load("deck-asr") is None

    first.save("deck-asr", AsrTaskRef(task_id="t1", token_id="k1"))
    second.save("deck-asr", AsrTaskRef(task_id="t2", token_id="k2"))

    assert first.load("deck-asr") == AsrTaskRef(task_id="t1", token_id="k1")
    assert second.load("deck-asr") == AsrTaskRef(task_id="t2", token_id="k2")
    assert JobMetadataAsrTaskStore().load("deck-asr") == AsrTaskRef(task_id="t0", token_id="k0")
    with get_sync_db_context() as session:
        job = session.get(Job, "deck-asr")
        assert job is not None
        assert job.job_metadata["document_id"] == "doc_deck"
