"""Persisted recovery eligibility and bounded dispatch against real PostgreSQL."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from shared.models.database.document import Document, DocumentScopeEnrichment
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.schemas.progressive_ingestion import (
    EnhancementStage,
    ProgressiveIngestionState,
)
from shared.services.jobs.progressive_ingestion import read_progressive_state
from shared.testing.contract_runtime import get_contract_database_url


@dataclass
class RecoveryHarness:
    engine: Engine
    now: datetime
    sent: list[tuple[str, list[object]]] = field(default_factory=list)

    def sweep(self) -> dict[str, object]:
        from shared.services.jobs.lifecycle.scope_enrichments import (
            sweep_progressive_enrichments_sync,
        )

        self.sent.clear()
        result = sweep_progressive_enrichments_sync()
        self.now += timedelta(seconds=30)
        return result

    def sent_revisions(self, task_suffix: str) -> list[str]:
        return [str(args[0]) for name, args in self.sent if name.endswith(task_suffix)]


@pytest.fixture
def recovery(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[RecoveryHarness, None, None]:
    from shared.core import celery_app as celery_module
    from shared.services.jobs.lifecycle import scope_enrichments

    del worker_contract_environment
    harness = RecoveryHarness(
        engine=create_engine(
            make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
        ),
        now=datetime(2026, 10, 29, 12),
    )

    class Broker:
        def send_task(self, name: str, args: list[object], **_options: object) -> None:
            harness.sent.append((name, args))

    monkeypatch.setattr(celery_module, "get_celery_app", Broker)
    monkeypatch.setattr(scope_enrichments, "utc_now_naive", lambda: harness.now)
    try:
        yield harness
    finally:
        harness.engine.dispose()


def seed_revision(
    db: Session,
    recovery: RecoveryHarness,
    *,
    suffix: str,
    scopes: list[tuple[str, str, int, datetime | None]],
    stages: dict[str, EnhancementStage] | None = None,
    is_structure_pending: bool = False,
    has_fresh_structure_claim: bool = False,
) -> str:
    owner: str = "bounded-recovery-owner"
    db.execute(
        text(
            'INSERT INTO "user" (id, name, email) VALUES (:owner, :owner, :email) '
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"owner": owner, "email": "bounded-recovery@test.local"},
    )
    job_id: str = f"job_{suffix}"
    revision_id: str = f"rev_{suffix}"
    document_id: str = f"doc_{suffix}"
    stale: datetime = recovery.now - timedelta(hours=3)
    job = Job(
        job_id=job_id,
        user_id=owner,
        job_type="document_ingestion",
        status="done",
        source_type="file",
        webhook_enabled=False,
        job_metadata={
            "namespace": "bounded-recovery",
            "parse_track": "page_memory",
            "source_file_name": "report.pdf",
            "enrichment_status": "text" if is_structure_pending else "coarse",
        },
        updated_at=stale,
    )
    db.add(job)
    db.flush()
    metadata: dict[str, object] = {}
    if stages is not None:
        metadata["progressive"] = ProgressiveIngestionState(stages=stages).model_dump(
            mode="json"
        )
    if has_fresh_structure_claim:
        metadata["structure_claimed_at"] = recovery.now.isoformat()
    result = JobResult(
        id=revision_id,
        job_id=job_id,
        delivery_mode="url",
        document_metadata=metadata,
        updated_at=stale,
    )
    db.add(result)
    db.flush()
    db.add(
        Document(
            document_id=document_id,
            user_id=owner,
            namespace="bounded-recovery",
            status="active",
            current_job_result_id=revision_id,
            source_file_name="report.pdf",
            parse_track="page_memory",
        )
    )
    db.flush()
    result.document_id = document_id
    db.add_all(
        DocumentScopeEnrichment(
            id=f"dse_{uuid4().hex[:12]}",
            document_id=document_id,
            job_result_id=revision_id,
            scope_id=scope_id,
            page_start=index + 1,
            page_end=index + 1,
            state=state,
            attempts=attempts,
            last_hit_at=last_hit,
            updated_at=stale,
        )
        for index, (scope_id, state, attempts, last_hit) in enumerate(scopes)
    )
    # Binding the result to its Document invokes the ORM's real-clock onupdate.
    # Restore a deterministic persisted age after that write for fairness tests.
    db.flush()
    result.updated_at = stale
    return revision_id


def completed_required_stages() -> dict[str, EnhancementStage]:
    return {
        "structure": {"status": "complete", "attempts": 1},
        "vector_repair": {"status": "complete", "attempts": 0},
        "finalization": {"status": "pending", "attempts": 0},
    }


def test_scope_scan_caps_dispatch_and_rotates_without_resending_fresh_queue(
    recovery: RecoveryHarness,
) -> None:
    with Session(recovery.engine) as db:
        seed_revision(
            db,
            recovery,
            suffix="scope-cap",
            scopes=[
                (f"p{index + 1}-{index + 1}", "coarse", 0, recovery.now)
                for index in range(205)
            ],
        )
        db.commit()
    observed: set[tuple[object, ...]] = set()
    for expected in (100, 100, 5, 0):
        result = recovery.sweep()
        sent: list[tuple[object, ...]] = [
            tuple(args)
            for name, args in recovery.sent
            if name.endswith("enrich_page_memory_scope")
        ]
        assert len(sent) == expected
        assert not observed.intersection(sent)
        observed.update(sent)
        assert len(result["retried_scopes"]) == expected
    assert len(observed) == 205


def test_expired_queued_scope_is_redispatched_and_claimable_but_recent_queue_is_not(
    recovery: RecoveryHarness,
) -> None:
    from app.services.page_memory.scope_enrichment_service import (
        _claim_and_validate_scope,
    )

    with Session(recovery.engine) as db:
        revision = seed_revision(
            db,
            recovery,
            suffix="queued-age",
            scopes=[("p1-1", "queued", 0, None), ("p2-2", "queued", 0, None)],
        )
        db.flush()
        recent = db.scalar(
            select(DocumentScopeEnrichment).where(
                DocumentScopeEnrichment.job_result_id == revision,
                DocumentScopeEnrichment.scope_id == "p2-2",
            )
        )
        assert recent is not None
        recent.updated_at = recovery.now
        db.commit()
    assert recovery.sweep()["retried_scopes"] == [(revision, "p1-1")]
    assert recovery.sweep()["retried_scopes"] == []
    status, target = _claim_and_validate_scope(job_result_id=revision, scope_id="p1-1")
    assert status == "claimed" and target is not None
    with Session(recovery.engine) as db:
        scope = db.scalar(
            select(DocumentScopeEnrichment).where(
                DocumentScopeEnrichment.job_result_id == revision,
                DocumentScopeEnrichment.scope_id == "p1-1",
            )
        )
        assert scope is not None and scope.state == "running" and scope.attempts == 1


def test_exhausted_scope_cleanup_is_bounded_and_still_reaches_terminal_failure(
    recovery: RecoveryHarness,
) -> None:
    from app.services.page_memory.scope_enrichment_service import (
        execute_progressive_recovery,
    )

    with Session(recovery.engine) as db:
        revision = seed_revision(
            db,
            recovery,
            suffix="exhausted-cap",
            stages=completed_required_stages(),
            scopes=[
                (f"p{index + 1}-{index + 1}", "running", 3, None)
                for index in range(205)
            ],
        )
        db.commit()
    for expected in (100, 200, 205):
        assert recovery.sweep()["retried_scopes"] == []
        with Session(recovery.engine) as db:
            failed = db.scalar(
                select(func.count())
                .select_from(DocumentScopeEnrichment)
                .where(
                    DocumentScopeEnrichment.job_result_id == revision,
                    DocumentScopeEnrichment.state == "failed",
                )
            )
            assert failed == expected
    assert execute_progressive_recovery(job_result_id=revision)["status"] == "failed"
    with Session(recovery.engine) as db:
        result = db.get(JobResult, revision)
        assert result is not None
        state = read_progressive_state(result.document_metadata)
        assert state is not None and state.enhancement.status == "failed"


def test_structure_scan_is_bounded_and_fresh_oldest_claims_do_not_starve_later_work(
    recovery: RecoveryHarness,
) -> None:
    eligible: set[str] = set()
    with Session(recovery.engine) as db:
        for index in range(205):
            revision = seed_revision(
                db,
                recovery,
                suffix=f"structure-{index:03}",
                scopes=[],
                is_structure_pending=True,
                has_fresh_structure_claim=index < 100,
            )
            if index >= 100:
                eligible.add(revision)
        db.commit()
    observed: set[str] = set()
    for expected in (0, 100, 5):
        recovery.sweep()
        sent = recovery.sent_revisions("build_page_memory_structure")
        assert len(sent) == expected
        assert not observed.intersection(sent)
        observed.update(sent)
    assert observed == eligible


def test_generic_scan_is_fair_and_idle_coarse_revisions_do_not_generate_tasks(
    recovery: RecoveryHarness,
) -> None:
    eligible: set[str] = set()
    with Session(recovery.engine) as db:
        for index in range(205):
            stages = completed_required_stages()
            if index >= 100:
                stages["vector_repair"] = {"status": "pending", "attempts": 0}
            revision = seed_revision(
                db,
                recovery,
                suffix=f"generic-{index:03}",
                stages=stages,
                scopes=[("p1-1", "coarse", 0, None)],
            )
            if index >= 100:
                eligible.add(revision)
        db.commit()
    observed: set[str] = set()
    for expected in (0, 100, 5):
        recovery.sweep()
        sent = recovery.sent_revisions("recover_progressive_revision")
        assert len(sent) == expected
        assert not observed.intersection(sent)
        observed.update(sent)
    assert observed == eligible


@pytest.mark.parametrize(
    (
        "stage_name",
        "status",
        "attempts",
        "has_fresh_claim",
        "is_refined",
        "should_send",
    ),
    [
        ("structure", "pending", 0, False, False, True),
        ("structure", "failed", 3, False, False, True),
        ("vector_repair", "failed", 1, False, False, True),
        ("vector_repair", "failed", 3, False, False, True),
        ("vector_repair", "running", 1, True, False, False),
        ("vector_repair", "running", 1, False, False, True),
        ("finalization", "failed", 1, False, True, True),
        ("finalization", "failed", 3, False, True, True),
        ("finalization", "pending", 0, False, True, True),
        ("finalization", "running", 1, True, True, False),
        ("finalization", "running", 1, False, True, True),
    ],
)
def test_generic_recovery_discovers_actionable_stages_and_preserves_fresh_claims(
    recovery: RecoveryHarness,
    stage_name: str,
    status: Literal["pending", "running", "failed"],
    attempts: int,
    has_fresh_claim: bool,
    is_refined: bool,
    should_send: bool,
) -> None:
    from app.services.page_memory.scope_enrichment_service import (
        _claim_progressive_stage,
    )

    stages = completed_required_stages()
    stage: EnhancementStage = {"status": status, "attempts": attempts}
    if status == "running":
        stage["claimed_at"] = (
            recovery.now if has_fresh_claim else recovery.now - timedelta(hours=2)
        ).isoformat()
    stages[stage_name] = stage
    with Session(recovery.engine) as db:
        revision = seed_revision(
            db,
            recovery,
            suffix="stage-eligibility",
            stages=stages,
            scopes=[("p1-1", "refined" if is_refined else "coarse", 0, None)],
        )
        db.commit()
    recovery.sweep()
    assert recovery.sent_revisions("recover_progressive_revision") == (
        [revision] if should_send else []
    )
    if attempts >= 3:
        assert (
            _claim_progressive_stage(job_result_id=revision, stage_name=stage_name)
            is None
        )
        with Session(recovery.engine) as db:
            result = db.get(JobResult, revision)
            assert result is not None
            state = read_progressive_state(result.document_metadata)
            assert state is not None and state.enhancement.status == "failed"


def test_structure_dispatch_does_not_also_enqueue_generic_recovery(
    recovery: RecoveryHarness,
) -> None:
    stages = completed_required_stages()
    stages["structure"] = {"status": "pending", "attempts": 0}
    with Session(recovery.engine) as db:
        revision = seed_revision(
            db,
            recovery,
            suffix="one-structure-dispatch",
            stages=stages,
            scopes=[],
            is_structure_pending=True,
        )
        db.commit()
    recovery.sweep()
    assert recovery.sent_revisions("build_page_memory_structure") == [revision]
    assert recovery.sent_revisions("recover_progressive_revision") == []
