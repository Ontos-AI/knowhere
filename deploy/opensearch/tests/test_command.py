"""Command plans stay offline unless execution is explicitly selected."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from test_operations import createInventory, createManifest


def runCommand(*arguments: str) -> subprocess.CompletedProcess[str]:
    command: Path = Path(__file__).parents[1] / "manage.py"
    assert command.is_file(), "OpenSearch operations command is not implemented"
    return subprocess.run([sys.executable, str(command), *arguments], capture_output=True, text=True, check=False)


def test_render_writes_immutable_bundle_receipt_and_no_cloud_execution(tmp_path: Path) -> None:
    manifest: Path = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(createManifest()))
    bundle: Path = tmp_path / "bundle"
    result = runCommand("render", "--manifest", str(manifest), "--directory", str(bundle))
    assert result.returncode == 0, result.stderr
    assert json.loads((bundle / "cloudformation.json").read_text())["Resources"]["NativeNode"]
    receipt = json.loads((bundle / "bundle-manifest.json").read_text())
    assert receipt["executed"] is False
    assert receipt["files"]["opensearch.yml"]
    assert receipt["files"]["config.yml"]
    assert receipt["files"]["client-environment.json"]
    assert receipt["files"]["installation-receipt.json"]
    assert runCommand("render", "--manifest", str(manifest), "--directory", str(bundle)).returncode != 0


def test_snapshot_submit_defaults_to_dryrun_and_requires_approval_before_native_io(tmp_path: Path) -> None:
    manifest: Path = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(createManifest()))
    catalog: Path = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(createInventory()))
    boundary: Path = tmp_path / "boundary.json"
    boundary.write_text(json.dumps({"documents": [], "updates": [], "deletes": []}))
    arguments: list[str] = ["snapshot-submit", "--manifest", str(manifest), "--inventory", str(catalog), "--boundary", str(boundary), "--snapshot", "hourly-test"]
    result = runCommand(*arguments)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mode"] == "dry-run"
    assert runCommand(*arguments, "--execute").returncode != 0


def test_installer_defaults_to_command_plan_without_root_or_artifacts(tmp_path: Path) -> None:
    manifest: Path = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(createManifest()))
    bundle: Path = tmp_path / "bundle"
    assert runCommand("render", "--manifest", str(manifest), "--directory", str(bundle)).returncode == 0
    result = runCommand("install", "--bundle", str(bundle), "--data-device", "/dev/disk/by-id/approved-volume", "--tls-directory", "/run/secrets/approved-native-tls")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["mode"] == "dry-run"
    assert plan["formats_volume"] is False
    assert plan["starts_service"] is False


def test_application_roles_exits_non_zero_when_live_roles_miss_actions(tmp_path: Path) -> None:
    manifest: Path = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(createManifest()))
    bundle: Path = tmp_path / "bundle"
    assert runCommand("render", "--manifest", str(manifest), "--directory", str(bundle)).returncode == 0
    complete: dict[str, object] = json.loads((bundle / "roles.yml").read_text())
    roles: Path = tmp_path / "roles.json"
    arguments: list[str] = ["application-roles", "--roles", str(roles), "--index-prefix", "knowhere-lexical-v1", "--role-prefix", "knowhere_"]

    roles.write_text(json.dumps(complete))
    result = runCommand(*arguments)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"] is True

    roles.write_text(json.dumps({**complete, "knowhere_worker": {}}))
    result = runCommand(*arguments)
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["missing"]["knowhere_worker"]
