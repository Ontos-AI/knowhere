"""Cutover prerequisites and measured abort budgets cannot be supplied by a dry-run."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import pytest


def loadRelease() -> type:
    directory: Path = Path(__file__).parents[1]
    path: Path = directory / "release.py"
    assert path.is_file(), "Two-release guarded operator adapter is not implemented"
    sys.path.insert(0, str(directory))
    specification = importlib.util.spec_from_file_location("native_release", path)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return cast(type, module.ReleaseOperations)


def test_dry_run_or_missing_qualification_receipts_cannot_authorize_cutover() -> None:
    release: type = loadRelease()
    outcome = release.qualify({}, sourceHash="a" * 64, manifestSha256="b" * 64)
    assert outcome["ready"] is False
    assert "mixed-load" in outcome["missing"]
    assert "native-recovery" in outcome["missing"]
    supplied: dict[str, object] = {name: {"mode": "dry-run", "status": "passed", "sourceHash": "a" * 64, "manifest_sha256": "b" * 64, "artifact_sha256": "c" * 64} for name in outcome["required"]}
    assert release.qualify(supplied, sourceHash="a" * 64, manifestSha256="b" * 64)["ready"] is False


def test_measures_restoration_from_first_impact_including_detection_and_earlier_attempts() -> None:
    release: type = loadRelease()
    receipt: dict[str, object] = {"mode": "actual", "status": "passed", "first_customer_impact_at": "2026-11-05T10:00:00+00:00", "service_restored_at": "2026-11-05T10:30:00+00:00", "current_published_coverage_complete": True, "canonical_api_smoke_passed": True, "includes_detection_human_response_and_failed_attempts": True}
    plan = release.plan(datetime(2026, 11, 5, 12, tzinfo=timezone.utc), receipt, marginSeconds=600)
    assert plan["measured_restoration_seconds"] == 1800
    assert plan["latest_abort_at"] == "2026-11-05T13:20:00+00:00"
    assert plan["executed"] is False
    receipt["mode"] = "dry-run"
    with pytest.raises(ValueError):
        release.plan(datetime(2026, 11, 5, 12, tzinfo=timezone.utc), receipt, marginSeconds=600)


def test_forward_work_stops_at_abort_time_while_rollback_remains_available() -> None:
    release: type = loadRelease()
    plan: dict[str, object] = {"latest_abort_at": "2026-11-05T13:20:00+00:00"}
    with pytest.raises(ValueError, match="abort"):
        release.assertWithinAbortBudget(plan, phase="forward", now=datetime(2026, 11, 5, 13, 20, tzinfo=timezone.utc))
    release.assertWithinAbortBudget(plan, phase="rollback", now=datetime(2026, 11, 5, 13, 20, tzinfo=timezone.utc))


def test_weaker_rollback_policy_cannot_target_opensearch() -> None:
    release: type = loadRelease()
    with pytest.raises(ValueError, match="PostgreSQL"):
        release.assertPhaseTarget(phase="rollback", targetBackend="opensearch")
    release.assertPhaseTarget(phase="rollback", targetBackend="postgresql")
    release.assertPhaseTarget(phase="forward", targetBackend="opensearch")
