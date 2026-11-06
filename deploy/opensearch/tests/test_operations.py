"""Reviewable deployment/recovery decisions without any cloud or native calls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import pytest


def loadOperations() -> type:
    path: Path = Path(__file__).parents[1] / "operations.py"
    assert path.is_file(), "OpenSearch operational tooling is not implemented"
    specification = importlib.util.spec_from_file_location("native_operations", path)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return cast(type, module.OpenSearchOperations)


def createManifest() -> dict[str, object]:
    return {
        "schema_version": "opensearch-installation-v1", "environment": "staging", "region": "us-east-1",
        "vpc_id": "vpc-0123456789abcdef0", "subnet_id": "subnet-0123456789abcdef0",
        "availability_zone": "us-east-1a", "ami_id": "ami-0123456789abcdef0",
        "api_security_group": "sg-00000000000000001",
        "worker_security_group": "sg-00000000000000002",
        "management_security_group": "sg-00000000000000003",
        "s3_prefix_list": "pl-0123456789abcdef0",
        "cluster_name": "knowhere-staging", "endpoint_host": "native.staging.internal",
        "index_prefix": "knowhere-lexical-v1", "node_dn": "CN=native.staging.internal",
        "admin_dn": "CN=knowhere-recovery-admin", "api_dn": "CN=knowhere-api", "worker_dn": "CN=knowhere-worker",
        "backup_dn": "CN=knowhere-backup", "monitor_dn": "CN=knowhere-monitor",
        "live_bucket": "approved-live-bucket", "backup_bucket": "approved-backup-bucket",
        "segment_prefix": "knowhere/live/segments", "translog_prefix": "knowhere/live/translogs",
        "state_prefix": "knowhere/live/state", "snapshot_prefix": "knowhere/full-snapshots",
        "archive_path": "/approved-cache/opensearch-3.9.0-linux-arm64.tar.gz", "archive_sha512": "a" * 128,
        "plugin_path": "/approved-cache/repository-s3-3.9.0.zip", "plugin_sha512": "b" * 128,
        "source_revision": "c" * 40,
    }


def test_renders_selected_private_single_node_and_retained_encrypted_data_volume() -> None:
    operations: type = loadOperations()
    bundle: dict[str, object] = operations.renderBundle(createManifest())
    template: dict[str, object] = cast(dict[str, object], bundle["cloudformation.json"])
    resources = template["Resources"]
    node = resources["NativeNode"]["Properties"]
    assert node["InstanceType"] == "r7g.xlarge"
    assert node["NetworkInterfaces"][0]["AssociatePublicIpAddress"] is False
    assert node["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"] == 20
    volume = resources["NativeDataVolume"]
    assert volume["DeletionPolicy"] == volume["UpdateReplacePolicy"] == "Retain"
    assert volume["Properties"]["Size"] == 100
    assert volume["Properties"]["Encrypted"] is True
    assert volume["Properties"]["Iops"] == 3000
    assert volume["Properties"]["Throughput"] == 125
    assert not any(resource["Type"] in ("AWS::EC2::NatGateway", "AWS::S3::Bucket", "AWS::KMS::Key") for resource in resources.values())


def test_renders_request_durability_remote_state_security_and_independent_full_backup() -> None:
    operations: type = loadOperations()
    bundle = operations.renderBundle(createManifest())
    configuration = bundle["opensearch.yml"]
    assert configuration["cluster.remote_store.state.enabled"] is True
    assert configuration["node.attr.remote_store.segment.repository"] == "knowhere-segments"
    assert configuration["node.attr.remote_store.translog.repository"] == "knowhere-translogs"
    assert configuration["plugins.security.ssl.http.enabled"] is True
    assert configuration["plugins.security.ssl.http.clientauth_mode"] == "REQUIRE"
    assert bundle["index-template.json"]["template"]["settings"]["index.translog.durability"] == "request"
    assert "-Xms16g -Xmx16g" in bundle["knowhere-opensearch.service"]
    repository = bundle["snapshot-repository.json"]["settings"]
    assert repository["remote_store_index_shallow_copy"] is False
    assert repository["shallow_snapshot_v2"] is False
    assert "cluster:admin/snapshot/restore" not in str(bundle["roles.yml"]["knowhere_api"])


def test_refuses_overlapping_repository_prefixes_and_unpinned_artifacts() -> None:
    operations: type = loadOperations()
    with pytest.raises(ValueError):
        operations.renderBundle({**createManifest(), "archive_sha512": "latest"})
    with pytest.raises(ValueError, match="overlap"):
        operations.renderBundle({**createManifest(), "translog_prefix": "knowhere/live"})


def createInventory() -> list[dict[str, object]]:
    return [
        {"name": "knowhere-lexical-v1-hot", "uuid": "uuid-hot", "state": "open", "primary_shards": 1},
        {"name": "knowhere-lexical-v1-cold", "uuid": "uuid-cold", "state": "closed", "primary_shards": 1},
    ]


def test_snapshot_explicitly_includes_closed_indexes_and_never_opens_them() -> None:
    operations: type = loadOperations()
    plan = operations.planSnapshot(createInventory(), boundarySha256="d" * 64, snapshotName="hourly-20261105-1200")
    assert plan["request"]["body"]["indices"] == [item["name"] for item in createInventory()]
    assert plan["request"]["body"]["ignore_unavailable"] is False
    assert plan["request"]["body"]["partial"] is False
    assert plan["request"]["body"]["expand_wildcards"] == "all"
    assert "_open" not in str(plan)
    assert plan["coverage_verified"] is False


def createSnapshot() -> dict[str, object]:
    return {
        "snapshot": "hourly-test", "uuid": "snapshot-uuid", "state": "SUCCESS", "indices": [item["name"] for item in createInventory()],
        "remote_store_index_shallow_copy": False, "failures": [],
        "start_time_in_millis": 1_000, "end_time_in_millis": 2_000,
        "shards": {"total": 2, "successful": 2, "failed": 0},
        "metadata": loadOperations().planSnapshot(createInventory(), boundarySha256="d" * 64, snapshotName="hourly-test")["request"]["body"]["metadata"],
    }


def createShardStatus() -> dict[str, object]:
    return {"snapshots": [{"snapshot": "hourly-test", "uuid": "snapshot-uuid", "state": "SUCCESS", "indices": {
        item["name"]: {"shards": {"0": {"stage": "DONE"}}} for item in createInventory()
    }}]}


@pytest.mark.parametrize("change", [
    {"state": "PARTIAL"}, {"indices": ["knowhere-lexical-v1-hot"]},
    {"shards": {"total": 1, "successful": 1, "failed": 0}},
    {"remote_store_index_shallow_copy": True},
])
def test_snapshot_success_without_complete_catalog_shard_and_full_copy_coverage_is_rejected(change: dict[str, object]) -> None:
    operations: type = loadOperations()
    outcome = operations.verifySnapshot(createInventory(), {**createSnapshot(), **change}, observedInventory=createInventory(), boundarySha256="d" * 64, verifiedBoundarySha256="d" * 64, shardStatus=createShardStatus(), verifiedAt=3_000)
    assert outcome["coverage_verified"] is False


def test_snapshot_boundary_identity_and_catalog_change_independently_fail_verification() -> None:
    operations: type = loadOperations()
    changed: list[dict[str, object]] = [{**createInventory()[0], "uuid": "new-uuid"}, createInventory()[1]]
    outcome = operations.verifySnapshot(createInventory(), createSnapshot(), observedInventory=changed, boundarySha256="d" * 64, verifiedBoundarySha256="e" * 64, shardStatus=createShardStatus(), verifiedAt=3_000)
    assert outcome["coverage_verified"] is False
    assert len(outcome["errors"]) >= 2
    success = operations.verifySnapshot(createInventory(), createSnapshot(), observedInventory=createInventory(), boundarySha256="d" * 64, verifiedBoundarySha256="d" * 64, shardStatus=createShardStatus(), verifiedAt=3_000, boundaryCapturedAtMillis=500)
    assert success["coverage_verified"] is True
    assert success["completed_at_millis"] == 2_000
    assert success["verified_at_millis"] == 3_000
    assert success["data_boundary_age_millis"] == 2_500


@pytest.mark.parametrize("metadata", [None, {}, {"inventory_sha256": "x" * 64, "boundary_sha256": "d" * 64}, {"inventory_sha256": "a" * 64, "boundary_sha256": "x" * 64}])
def test_snapshot_metadata_must_bind_the_actual_catalog_and_boundary(metadata: object) -> None:
    operations: type = loadOperations()
    outcome = operations.verifySnapshot(createInventory(), {**createSnapshot(), "metadata": metadata},
        observedInventory=createInventory(), boundarySha256="d" * 64, verifiedBoundarySha256="d" * 64,
        shardStatus=createShardStatus(), verifiedAt=3_000, boundaryCapturedAtMillis=500)
    assert outcome["coverage_verified"] is False
    assert any("metadata" in error for error in outcome["errors"])


def test_backup_requires_per_index_done_shards_not_only_aggregate_success() -> None:
    operations: type = loadOperations()
    status: dict[str, object] = createShardStatus()
    status["snapshots"][0]["indices"]["knowhere-lexical-v1-cold"]["shards"]["0"]["stage"] = "FAILURE"
    result = operations.verifySnapshot(createInventory(), createSnapshot(), observedInventory=createInventory(), boundarySha256="d" * 64, verifiedBoundarySha256="d" * 64, shardStatus=status, verifiedAt=3_000)
    assert result["coverage_verified"] is False


def test_retention_keeps_three_verified_points_even_when_older_than_seven_days() -> None:
    operations: type = loadOperations()
    now: int = 30 * 86_400_000
    points: list[dict[str, object]] = [
        {"snapshot": f"verified-{index}", "state": "SUCCESS", "coverage_verified": True, "completed_at_millis": index * 86_400_000}
        for index in range(1, 5)
    ]
    points.append({"snapshot": "recent-partial", "state": "PARTIAL", "coverage_verified": False, "completed_at_millis": now - 1_000})
    outcome = operations.planRetention(points, nowMillis=now)
    assert outcome["protected"] == ["verified-4", "verified-3", "verified-2"]
    assert outcome["delete"] == ["verified-1"]
    assert outcome["deletion_api"] == "DELETE /_snapshot/knowhere-full/{snapshot}"


def test_restore_reserves_before_each_batch_and_counts_open_reserved_uncertain() -> None:
    operations: type = loadOperations()
    plan = operations.planRestore([f"index-{index}" for index in range(45)], openCount=350, reservedCount=20, uncertainCount=10, batchLimit=32)
    assert [len(batch["indices"]) for batch in plan["batches"]] == [20, 20, 5]
    assert plan["batches"][0]["actions"] == ["reserve", "restore", "verify-identities-and-boundary", "close-idle", "reconcile-and-release"]
    assert plan["recheck_capacity_before_each_batch"] is True
    with pytest.raises(ValueError, match="capacity"):
        operations.planRestore(["index-one"], openCount=400, reservedCount=0, uncertainCount=0, batchLimit=32)


def test_recovery_refuses_unfenced_issuer_or_missing_current_published_boundary() -> None:
    operations: type = loadOperations()
    receipt: dict[str, object] = {"old_issuer_stopped": False, "application_gated": True, "native_writers_settled": True, "old_epoch": 3, "new_epoch": 4, "proof_sha256": "a" * 64}
    with pytest.raises(ValueError, match="fenced"):
        operations.planRecovery("full-snapshot", receipt, requiredBoundary="b" * 64, restoredBoundary="c" * 64)
    receipt["old_issuer_stopped"] = True
    plan = operations.planRecovery("full-snapshot", receipt, requiredBoundary="b" * 64, restoredBoundary="c" * 64)
    assert plan["historical_snapshot_restored"] is False
    assert plan["historical_boundary_supplied"] is True
    assert plan["full_service_restored"] is False
    assert plan["reopen_traffic"] is False
    assert plan["rebuild_from_postgresql"] is False


def test_offnode_alerts_throttle_resolve_and_rearm_without_state_mutation() -> None:
    operations: type = loadOperations()
    metrics: dict[str, object] = {"native_available": False, "indexing_oldest_seconds": 301, "pg_copy_oldest_seconds": 1801, "latest_complete_snapshot_age_seconds": 7201, "backup_incomplete": True}
    first = operations.evaluateAlerts(metrics, {}, nowSeconds=1_000, repeatSeconds=900)
    assert {event["condition"] for event in first["events"]} == {"native-unavailable", "indexing-age", "pg-copy-age", "backup-age", "backup-incomplete"}
    second = operations.evaluateAlerts(metrics, first["state"], nowSeconds=1_001, repeatSeconds=900)
    assert second["events"] == []
    healthy: dict[str, object] = {"native_available": True, "indexing_oldest_seconds": 0, "pg_copy_oldest_seconds": 0, "latest_complete_snapshot_age_seconds": 0, "backup_incomplete": False}
    resolved = operations.evaluateAlerts(healthy, second["state"], nowSeconds=1_002, repeatSeconds=900)
    assert all(event["transition"] == "resolved" for event in resolved["events"])
    rearmed = operations.evaluateAlerts(metrics, resolved["state"], nowSeconds=1_003, repeatSeconds=900)
    assert all(event["transition"] == "firing" for event in rearmed["events"])
    assert rearmed["mutates_job_or_backend"] is False


def test_release_abort_budget_uses_measured_restoration_and_explicit_margin() -> None:
    operations: type = loadOperations()
    impact: datetime = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    outcome = operations.planRelease(impact, restorationSeconds=1_800, marginSeconds=600)
    assert outcome["latest_abort_at"] == (impact + timedelta(seconds=4_800)).isoformat()
    assert "settle" in outcome["forward_verbs"]
    assert outcome["automatic_retry_after_resume"] is False
    assert outcome["schema_downgrade"] is False
    with pytest.raises(ValueError):
        operations.planRelease(impact, restorationSeconds=7_000, marginSeconds=600)


def test_matching_boundary_alone_cannot_claim_completed_recovery_or_reopen_traffic() -> None:
    operations: type = loadOperations()
    proof: dict[str, object] = {"old_issuer_stopped": True, "application_gated": True, "native_writers_settled": True, "old_epoch": 3, "new_epoch": 4, "proof_sha256": "a" * 64}
    plan = operations.planRecovery("healthy-volume", proof, requiredBoundary="b" * 64, restoredBoundary="b" * 64)
    assert plan["full_service_restored"] is False
    assert plan["reopen_traffic"] is False
    assert plan["published_boundary_matches"] is True


def test_cost_plan_requires_complete_current_quote_and_bounds_recovery_capacity() -> None:
    operations: type = loadOperations()
    quote: dict[str, object] = {
        "as_of": "2026-11-05", "source": "https://aws.amazon.com/ec2/pricing/on-demand/",
        "compute_hour": 0.1, "gp3_gib_month": 0.1, "s3_gib_month": 0.1,
        "put_per_thousand": 0.1, "get_per_thousand": 0.1, "network_gib": 0.1,
        "kms_per_ten_thousand": 0.1,
    }
    usage: dict[str, object] = {"remote_gib": 10, "snapshot_gib": 20, "put_requests": 2_000, "get_requests": 3_000, "network_gib": 0, "kms_requests": 0, "temporary_recovery_hours": 2,
        "temporary_recovery_extra_gib": 120, "temporary_recovery_s3_gib_month_equivalent": 0,
        "temporary_recovery_put_requests": 0, "temporary_recovery_get_requests": 0,
        "temporary_recovery_network_gib": 0, "temporary_recovery_kms_requests": 0}
    result = operations.planCost(quote, usage)
    assert result["monthly_estimated_usd"] == pytest.approx(88.5)
    assert result["temporary_recovery_estimated_usd"] == pytest.approx(0.232877)
    assert result["quote_is_actual_bill"] is False
    assert result["approved_to_provision"] is False
    with pytest.raises(ValueError):
        operations.planCost({}, usage)


def test_preflight_binds_exact_manifest_and_cost_and_never_infers_approval() -> None:
    operations: type = loadOperations()
    result = operations.preflight(createManifest(), proofs={})
    assert result["ready_to_provision"] is False
    assert "explicit-cost-approval" in result["missing"]
    assert "private-S3-gateway-route-and-policies" in result["missing"]
    assert "ARM64-AMI-and-pinned-plugin-artifacts" in result["missing"]
    assert result["manifest_sha256"]


def test_scheduler_templates_keep_monitor_outside_the_hourly_backup_and_worker_queue() -> None:
    operations: type = loadOperations()
    bundle = operations.renderBundle(createManifest())
    assert "OnCalendar=*-*-* *:*:00" in bundle["knowhere-native-backup.timer"]
    assert "Persistent=true" in bundle["knowhere-native-backup.timer"]
    assert "monitor-check.sh" in bundle["knowhere-native-monitor.service"]
    assert "monitor.py" in bundle["monitor-check.sh"]
    assert "celery" not in bundle["knowhere-native-monitor.service"].lower()
    assert "OnUnitActiveSec=60s" in bundle["knowhere-native-monitor.timer"]


def test_independent_full_restore_configuration_cannot_bootstrap_from_live_repositories() -> None:
    operations: type = loadOperations()
    bundle = operations.renderBundle(createManifest())
    independent = bundle["full-restore-opensearch.yml"]
    assert independent["cluster.remote_store.state.enabled"] is False
    assert not any(key.startswith("node.attr.remote_store.") for key in independent)
    assert bundle["full-restore-repository.json"]["settings"]["readonly"] is True
    assert bundle["deny-live-repositories-for-independent-restore.json"]["Statement"]
