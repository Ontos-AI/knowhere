"""Contracts for sealed PostgreSQL template clone acceleration."""

# ruff: noqa: E402

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Sequence

import pytest

from tests.support.publication_benchmark_support import (
    benchmark_layout,
    ensure_benchmark_import_path,
)

ensure_benchmark_import_path()

from scripts.publication_benchmark.clone_db import (  # noqa: E402
    CloneCommandError,
    CommandResult,
    PostgresCloneManager,
    build_argument_parser,
)
from scripts.publication_benchmark.clone_state import (  # noqa: E402
    CloneRecord,
    clone_data_volume_name,
    read_clone_record,
    write_clone_record,
)

TEMPLATE_ID = "template-contract"
SAMPLE_ID = "sample-contract"
TEMPLATE_DIGEST = "a" * 64


class TemplateCommandRunner:
    """Capture clone commands without mutating Docker resources."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.piped: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.is_template_running = False
        self.has_external_tablespace = False
        self.fail_copy = False
        self.target_exists = False
        self.cluster_state = "shut down"
        self.fail_volume_removal = False
        self.fail_index_inventory = False
        self.fail_target_inspect = False
        self.active_sessions = 0

    def __call__(
        self,
        command: Sequence[str],
        *,
        stdin: bytes | None = None,
        check: bool = True,
    ) -> CommandResult:
        del stdin
        parts = tuple(command)
        self.commands.append(parts)
        if parts[:3] == ("docker", "volume", "rm") and self.fail_volume_removal:
            if check:
                raise CloneCommandError("volume removal failed")
            return CommandResult(returncode=1, stdout="", stderr="busy")
        if parts[:3] == ("docker", "volume", "inspect") and parts[-1] in (
            clone_data_volume_name(SAMPLE_ID),
            clone_data_volume_name(TEMPLATE_ID),
        ):
            if self.fail_target_inspect:
                return CommandResult(
                    returncode=1, stdout="", stderr="permission denied"
                )
            return CommandResult(
                returncode=0
                if self.target_exists and parts[-1] == clone_data_volume_name(SAMPLE_ID)
                else 1,
                stdout="",
                stderr=""
                if self.target_exists and parts[-1] == clone_data_volume_name(SAMPLE_ID)
                else "No such volume",
            )
        if parts[:3] == ("docker", "container", "inspect") and parts[-1].endswith(
            (SAMPLE_ID, TEMPLATE_ID)
        ):
            return CommandResult(returncode=1, stdout="", stderr="No such container")
        if "{{.State.Running}}" in parts:
            return CommandResult(
                returncode=0,
                stdout="true\n" if self.is_template_running else "false\n",
                stderr="",
            )
        if "{{json .NetworkSettings.Ports}}" in parts:
            return CommandResult(
                returncode=0,
                stdout='{"5432/tcp":[{"HostIp":"127.0.0.1","HostPort":"55433"}]}\n',
                stderr="",
            )
        if "pg_controldata /from" in parts[-1]:
            if self.has_external_tablespace:
                raise CloneCommandError("external tablespace present")
            return CommandResult(
                returncode=0,
                stdout=f"15\nDatabase cluster state:               {self.cluster_state}\n",
                stderr="",
            )
        if "tar -C /from -cf - . | sha256sum" in parts[-1]:
            return CommandResult(
                returncode=0, stdout=f"{TEMPLATE_DIGEST}  -\n", stderr=""
            )
        if "cp -a /from/. /to/; sync" in parts[-1] and self.fail_copy:
            if check:
                raise CloneCommandError("template copy failed")
            return CommandResult(returncode=1, stdout="", stderr="copy failed")
        if "pg_isready" in parts:
            return CommandResult(
                returncode=0, stdout="accepting connections\n", stderr=""
            )
        if "FROM pg_stat_activity" in parts[-1]:
            return CommandResult(
                returncode=0,
                stdout=f"{self.active_sessions}\n",
                stderr="",
            )
        if "alembic_version" in parts[-1]:
            return CommandResult(returncode=0, stdout="20260305_baseline\n", stderr="")
        if "pg_control_system" in parts[-1]:
            return CommandResult(
                returncode=0,
                stdout="7000000000000000001\tknowhere_benchmark\t16384\t4096\n",
                stderr="",
            )
        if "FROM pg_indexes" in parts[-1]:
            if self.fail_index_inventory:
                raise CloneCommandError("index inventory failed")
            return CommandResult(
                returncode=0,
                stdout="public\tdocuments\tix_documents\t8192\n",
                stderr="",
            )
        if "SHOW " in parts[-1]:
            return CommandResult(returncode=0, stdout="15.17\n", stderr="")
        if "sha256sum /from/PG_VERSION" in parts[-1]:
            return CommandResult(
                returncode=0, stdout="volume-inventory-hash\n", stderr=""
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    def pipe(
        self,
        source_command: Sequence[str],
        target_command: Sequence[str],
    ) -> None:
        self.piped.append((tuple(source_command), tuple(target_command)))


def create_sealed_template(
    tmp_path: Path,
    runner: TemplateCommandRunner,
) -> PostgresCloneManager:
    manager = PostgresCloneManager(
        layout=benchmark_layout(tmp_path),
        runner=runner,
    )
    manager.create(run_id=TEMPLATE_ID, profile="production")
    manager.seal_template(run_id=TEMPLATE_ID)
    return manager


def test_sealed_template_copies_full_pgdata_without_repeating_restore(
    tmp_path: Path,
) -> None:
    runner = TemplateCommandRunner()
    manager = create_sealed_template(tmp_path, runner)
    layout = benchmark_layout(tmp_path)
    assert not layout.clone_record_path(TEMPLATE_ID).exists()
    assert not layout.database_url_path(TEMPLATE_ID).exists()
    assert len(runner.piped) == 1

    record = manager.create(
        run_id=SAMPLE_ID,
        profile="production",
        template_id=TEMPLATE_ID,
    )
    assert record.state == "ready"
    assert record.data_volume == clone_data_volume_name(SAMPLE_ID)
    assert record.source.volume_name == "knowhere-prod-restore-data"
    assert record.database_identity["template_id"] == TEMPLATE_ID
    assert (
        record.database_identity["template_content_digest"]
        == f"sha256:{TEMPLATE_DIGEST}"
    )
    assert len(runner.piped) == 1
    assert (
        sum(
            "VACUUM (ANALYZE)" in command
            for parts in runner.commands
            for command in parts
        )
        == 1
    )
    copy_commands = [
        parts for parts in runner.commands if "cp -a /from/. /to/; sync" in parts[-1]
    ]
    assert len(copy_commands) == 1
    assert f"{clone_data_volume_name(TEMPLATE_ID)}:/from:ro" in copy_commands[0]
    assert f"{clone_data_volume_name(SAMPLE_ID)}:/to" in copy_commands[0]

    reset_record = manager.reset(run_id=SAMPLE_ID, template_id=TEMPLATE_ID)
    assert reset_record.state == "reset"
    assert len(runner.piped) == 1
    assert (
        len(
            [
                parts
                for parts in runner.commands
                if "cp -a /from/. /to/; sync" in parts[-1]
            ]
        )
        == 2
    )


def test_template_guards_refuse_running_or_external_cluster(
    tmp_path: Path,
) -> None:
    runner = TemplateCommandRunner()
    manager = create_sealed_template(tmp_path, runner)
    runner.is_template_running = True
    with pytest.raises(CloneCommandError, match="running"):
        manager.create(run_id=SAMPLE_ID, profile="production", template_id=TEMPLATE_ID)

    runner.is_template_running = False
    runner.cluster_state = "in production"
    with pytest.raises(CloneCommandError, match="not shut down"):
        manager.create(run_id=SAMPLE_ID, profile="production", template_id=TEMPLATE_ID)

    runner.cluster_state = "shut down"
    runner.has_external_tablespace = True
    with pytest.raises(CloneCommandError, match="external tablespace"):
        manager.create(run_id=SAMPLE_ID, profile="production", template_id=TEMPLATE_ID)
    assert not any(
        parts[:3] == ("docker", "volume", "create")
        and parts[-1] == clone_data_volume_name(SAMPLE_ID)
        for parts in runner.commands
    )


def test_seal_template_refuses_other_database_sessions_before_stopping(
    tmp_path: Path,
) -> None:
    runner = TemplateCommandRunner()
    layout = benchmark_layout(tmp_path)
    manager = PostgresCloneManager(layout=layout, runner=runner)
    manager.create(run_id=TEMPLATE_ID, profile="production")
    runner.active_sessions = 1

    with pytest.raises(CloneCommandError, match="other active database sessions"):
        manager.seal_template(run_id=TEMPLATE_ID)

    assert any(
        "FROM pg_stat_activity" in command
        and "backend_type = 'client backend'" in command
        and "datname = current_database()" not in command
        for parts in runner.commands
        for command in parts
    )
    assert not any(parts[:2] == ("docker", "stop") for parts in runner.commands)
    assert read_clone_record(layout.clone_record_path(TEMPLATE_ID)).state == "ready"


def test_template_guard_refuses_existing_sample_target(tmp_path: Path) -> None:
    runner = TemplateCommandRunner()
    manager = create_sealed_template(tmp_path, runner)
    runner.target_exists = True

    with pytest.raises(CloneCommandError, match="target already exists"):
        manager.create(run_id=SAMPLE_ID, profile="production", template_id=TEMPLATE_ID)

    assert not any(
        parts[:3] == ("docker", "volume", "rm")
        and parts[-1] == clone_data_volume_name(SAMPLE_ID)
        for parts in runner.commands
    )


def test_inspect_error_cannot_be_mistaken_for_absent_target(tmp_path: Path) -> None:
    runner = TemplateCommandRunner()
    manager = create_sealed_template(tmp_path, runner)
    runner.fail_target_inspect = True

    with pytest.raises(CloneCommandError, match="cannot verify"):
        manager.create(run_id=SAMPLE_ID, profile="production", template_id=TEMPLATE_ID)

    assert not any(
        parts[:3] == ("docker", "volume", "create")
        and parts[-1] == clone_data_volume_name(SAMPLE_ID)
        for parts in runner.commands
    )


def test_template_guards_protect_identity_and_partial_copy(
    tmp_path: Path,
) -> None:
    runner = TemplateCommandRunner()
    manager = create_sealed_template(tmp_path, runner)
    with pytest.raises(CloneCommandError, match="identities must differ"):
        manager.create(
            run_id=TEMPLATE_ID, profile="production", template_id=TEMPLATE_ID
        )

    runner.fail_copy = True
    with pytest.raises(CloneCommandError, match="copy failed"):
        manager.create(run_id=SAMPLE_ID, profile="production", template_id=TEMPLATE_ID)
    assert not benchmark_layout(tmp_path).clone_record_path(SAMPLE_ID).exists()
    assert any(
        parts[:3] == ("docker", "volume", "rm")
        and parts[-1] == clone_data_volume_name(SAMPLE_ID)
        for parts in runner.commands
    )
    assert not any(
        parts[:3] == ("docker", "volume", "rm")
        and parts[-1] == clone_data_volume_name(TEMPLATE_ID)
        for parts in runner.commands
    )


def test_template_volume_cannot_be_destroyed_through_clone_lifecycle(
    tmp_path: Path,
) -> None:
    runner = TemplateCommandRunner()
    manager = create_sealed_template(tmp_path, runner)
    layout = benchmark_layout(tmp_path)
    template_payload = manager._template_record_path(TEMPLATE_ID)
    payload = json.loads(template_payload.read_text(encoding="utf-8"))
    write_clone_record(
        layout.clone_record_path(TEMPLATE_ID),
        CloneRecord.from_dict(payload["source_clone"]),
    )
    with pytest.raises(CloneCommandError, match="sealed template volume"):
        manager.destroy(run_id=TEMPLATE_ID)
    assert not any(
        parts[:3] == ("docker", "volume", "rm")
        and parts[-1] == clone_data_volume_name(TEMPLATE_ID)
        for parts in runner.commands
    )


def test_partial_seal_directory_still_protects_template_volume(
    tmp_path: Path,
) -> None:
    runner = TemplateCommandRunner()
    manager = create_sealed_template(tmp_path, runner)
    layout = benchmark_layout(tmp_path)
    template_path = manager._template_record_path(TEMPLATE_ID)
    payload = json.loads(template_path.read_text(encoding="utf-8"))
    template_path.unlink()
    write_clone_record(
        layout.clone_record_path(TEMPLATE_ID),
        CloneRecord.from_dict(payload["source_clone"]),
    )

    with pytest.raises(CloneCommandError, match="sealed template volume"):
        manager.destroy(run_id=TEMPLATE_ID)
    assert not any(
        parts[:3] == ("docker", "volume", "rm")
        and parts[-1] == clone_data_volume_name(TEMPLATE_ID)
        for parts in runner.commands
    )


def test_destroy_refuses_forged_source_container_and_failed_volume_removal(
    tmp_path: Path,
) -> None:
    runner = TemplateCommandRunner()
    manager = create_sealed_template(tmp_path, runner)
    layout = benchmark_layout(tmp_path)
    manager.create(run_id=SAMPLE_ID, profile="production", template_id=TEMPLATE_ID)
    record_path = layout.clone_record_path(SAMPLE_ID)
    original = read_clone_record(record_path)

    write_clone_record(
        record_path, replace(original, container_name="knowhere-prod-restore-pg")
    )
    with pytest.raises(CloneCommandError, match="managed target"):
        manager.destroy(run_id=SAMPLE_ID)
    assert not any(
        parts[:2] == ("docker", "rm") and parts[-1] == "knowhere-prod-restore-pg"
        for parts in runner.commands
    )
    with pytest.raises(CloneCommandError, match="September source container"):
        manager._delete_clone_resources(
            clone_volume=clone_data_volume_name(SAMPLE_ID),
            clone_container="knowhere-prod-restore-pg",
        )

    write_clone_record(record_path, original)
    runner.fail_volume_removal = True
    with pytest.raises(CloneCommandError, match="volume removal failed"):
        manager.destroy(run_id=SAMPLE_ID)
    assert read_clone_record(record_path).state == "failed"


def test_failed_reset_cannot_recreate_over_remaining_volume(tmp_path: Path) -> None:
    runner = TemplateCommandRunner()
    manager = create_sealed_template(tmp_path, runner)
    layout = benchmark_layout(tmp_path)
    manager.create(run_id=SAMPLE_ID, profile="production", template_id=TEMPLATE_ID)
    prior_copies = sum(
        "cp -a /from/. /to/; sync" in parts[-1] for parts in runner.commands
    )
    runner.fail_volume_removal = True

    with pytest.raises(CloneCommandError, match="volume removal failed"):
        manager.reset(run_id=SAMPLE_ID, template_id=TEMPLATE_ID)

    assert read_clone_record(layout.clone_record_path(SAMPLE_ID)).state == "failed"
    assert (
        sum("cp -a /from/. /to/; sync" in parts[-1] for parts in runner.commands)
        == prior_copies
    )


def test_template_create_cleans_resources_after_introspection_failure(
    tmp_path: Path,
) -> None:
    runner = TemplateCommandRunner()
    manager = create_sealed_template(tmp_path, runner)
    runner.fail_index_inventory = True

    with pytest.raises(CloneCommandError, match="index inventory failed"):
        manager.create(run_id=SAMPLE_ID, profile="production", template_id=TEMPLATE_ID)

    layout = benchmark_layout(tmp_path)
    assert not layout.clone_record_path(SAMPLE_ID).exists()
    assert not layout.database_url_path(SAMPLE_ID).exists()
    assert any(
        parts[:3] == ("docker", "volume", "rm")
        and parts[-1] == clone_data_volume_name(SAMPLE_ID)
        for parts in runner.commands
    )


def test_reset_restores_failed_record_when_new_template_copy_fails(
    tmp_path: Path,
) -> None:
    runner = TemplateCommandRunner()
    manager = create_sealed_template(tmp_path, runner)
    layout = benchmark_layout(tmp_path)
    manager.create(run_id=SAMPLE_ID, profile="production", template_id=TEMPLATE_ID)
    runner.fail_copy = True

    with pytest.raises(CloneCommandError, match="copy failed"):
        manager.reset(run_id=SAMPLE_ID, template_id=TEMPLATE_ID)

    failed_record = read_clone_record(layout.clone_record_path(SAMPLE_ID))
    assert failed_record.state == "failed"
    assert failed_record.source.volume_name == "knowhere-prod-restore-data"
    assert failed_record.database_identity["template_id"] == TEMPLATE_ID


def test_template_cli_is_explicit_opt_in() -> None:
    parser = build_argument_parser()
    default = parser.parse_args(["--run-id", SAMPLE_ID])
    assert default.action == "create"
    assert default.template_id is None
    template = parser.parse_args(
        ["--run-id", SAMPLE_ID, "--template-id", TEMPLATE_ID, "--action", "reset"]
    )
    assert template.template_id == TEMPLATE_ID
    assert template.action == "reset"
