"""Contracts for the manual staging ECS management workflow."""

from pathlib import Path


WORKFLOW_PATH: Path = (
    Path(__file__).parents[2] / ".github/workflows/manage-staging.yml"
)


def _read_workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_workflow_exposes_only_manual_status_start_and_stop() -> None:
    """Availability changes require a deliberate workflow dispatch."""
    workflow: str = _read_workflow()

    assert "workflow_dispatch:" in workflow
    assert "          - status\n          - start\n          - stop" in workflow
    assert "schedule:" not in workflow
    assert "--approval-reference" not in workflow
    assert 'if [ "$OPERATION" != "status" ]' in workflow


def test_mutations_require_an_ontos_ai_github_approval_url() -> None:
    """A free-form audit note must not be accepted as mutation approval."""
    workflow: str = _read_workflow()

    assert "approval_reference_pattern=" in workflow
    assert '[[ ! "$APPROVAL_REFERENCE" =~ $approval_reference_pattern ]]' in workflow
    assert "Ontos-AI GitHub issue, pull request, or comment URL" in workflow


def test_workflow_uses_named_profile_and_approved_account() -> None:
    """Every live AWS call is pinned to the Knowhere account and profile."""
    workflow: str = _read_workflow()

    assert "aws --profile knowhere sts get-caller-identity" in workflow
    assert 'if [ "$account_id" != "107424103509" ]' in workflow
    assert "aws ecs" not in workflow
    assert "aws sts" not in workflow
    assert "aws secretsmanager" not in workflow


def test_start_restores_healthy_workers_before_api() -> None:
    """The public API cannot open before the two-worker floor is healthy."""
    workflow: str = _read_workflow()
    start_block: str = workflow.split("            start)", maxsplit=1)[1].split(
        "              ;;", maxsplit=1
    )[0]

    assert start_block.index("update_service knowhere-worker-staging 2") < (
        start_block.index("wait_for_service knowhere-worker-staging")
    )
    assert start_block.index("wait_for_service knowhere-worker-staging") < (
        start_block.index('if [ "$healthy_workers" -ne 2 ]')
    )
    assert start_block.index('if [ "$healthy_workers" -ne 2 ]') < (
        start_block.index("update_service knowhere-api-staging 1")
    )
    assert start_block.index("update_service knowhere-api-staging 1") < (
        start_block.index("https://api-staging.knowhereto.ai/health")
    )


def test_stop_closes_api_then_drains_before_workers() -> None:
    """The stop operation preserves the accepted API-first 30-minute drain."""
    workflow: str = _read_workflow()
    stop_block: str = workflow.split("            stop)", maxsplit=1)[1].split(
        "              ;;", maxsplit=1
    )[0]

    assert stop_block.index("update_service knowhere-api-staging 0") < (
        stop_block.index("sleep 1800")
    )
    assert stop_block.index("sleep 1800") < stop_block.index(
        "update_service knowhere-worker-staging 0"
    )


def test_status_uses_the_jobs_ledger_for_backlog() -> None:
    """Status reports durable work without requiring private Redis access."""
    workflow: str = _read_workflow()

    assert "FROM jobs" in workflow
    assert "WHERE status NOT IN ('done', 'failed')" in workflow
    assert "'queued'" in workflow
    assert "status IN ('pending', 'waiting-file')" in workflow
    assert "'processing'" in workflow
    assert "status IN ('running', 'converting')" in workflow
    assert "get-secret-value" in workflow


def test_start_reports_readiness_time() -> None:
    """Operators can compare cold-start readiness with the schedule lead time."""
    workflow: str = _read_workflow()

    assert 'start_started_epoch="$(date +%s)"' in workflow
    assert 'startup_seconds="$(($(date +%s) - start_started_epoch))"' in workflow
    assert "startupSeconds" in workflow
