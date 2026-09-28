"""Contract tests for writable benchmark clone creation and refusal rules."""

# ruff: noqa: E402

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

import pytest

from tests.support.publication_benchmark_support import (
    SOURCE_CONTAINER_NAME,
    SOURCE_DATABASE_NAME,
    SOURCE_DATABASE_PORT,
    SOURCE_VOLUME_NAME,
    benchmark_layout,
    build_clone_record,
    ensure_benchmark_import_path,
    write_listed_clone,
)

ensure_benchmark_import_path()

from scripts.publication_benchmark.clone_db import (  # noqa: E402
    CloneCommandError,
    CommandResult,
    PostgresCloneManager,
)
from scripts.publication_benchmark.clone_state import (  # noqa: E402
    CloneRecord,
    CloneRecordError,
    SourceVolumeIdentity,
    clone_data_volume_name,
    find_listed_clone,
    read_clone_record,
)
from scripts.publication_benchmark.guards import (  # noqa: E402
    BenchmarkGuardError,
    assert_not_source_database,
    assert_writable_database_url,
    read_database_url_file,
)

RUN_ID = "clone-contract-run"


class FakeCommandRunner:
    """Deterministic Docker runner used by the clone contract tests."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.piped: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.responses: list[tuple[Callable[[Sequence[str]], bool], CommandResult]] = []

    def add_response(
        self,
        predicate: Callable[[Sequence[str]], bool],
        result: CommandResult,
    ) -> None:
        self.responses.append((predicate, result))

    def __call__(
        self,
        command: Sequence[str],
        *,
        stdin: bytes | None = None,
        check: bool = True,
    ) -> CommandResult:
        self.commands.append(tuple(command))
        for predicate, result in self.responses:
            if predicate(command):
                if check and result.returncode != 0:
                    raise CloneCommandError(f"fake command failed: {' '.join(command)}")
                return result
        return CommandResult(returncode=0, stdout="", stderr="")

    def pipe(
        self,
        source_command: Sequence[str],
        target_command: Sequence[str],
    ) -> None:
        self.piped.append((tuple(source_command), tuple(target_command)))

    def commands_containing(self, needle: str) -> list[tuple[str, ...]]:
        return [command for command in self.commands if needle in command]


def build_fake_runner() -> FakeCommandRunner:
    runner = FakeCommandRunner()
    runner.add_response(
        lambda command: (
            tuple(command[:3])
            in (
                ("docker", "volume", "inspect"),
                ("docker", "container", "inspect"),
            )
            and str(command[-1]).startswith("knowhere-bench-clone-")
        ),
        CommandResult(returncode=1, stdout="", stderr="not found"),
    )
    runner.add_response(
        lambda command: command[:4] == ("docker", "run", "--rm", "-v"),
        CommandResult(returncode=0, stdout="volume-inventory-hash\n", stderr=""),
    )
    runner.add_response(
        lambda command: "inspect" in command and "--format" in command,
        CommandResult(
            returncode=0,
            stdout=(
                '{"5432/tcp":[{"HostIp":"0.0.0.0","HostPort":"55433"},'
                '{"HostIp":"::","HostPort":"55433"}]}\n'
            ),
            stderr="",
        ),
    )
    runner.add_response(
        lambda command: "pg_isready" in command,
        CommandResult(returncode=0, stdout="accepting connections\n", stderr=""),
    )
    runner.add_response(
        lambda command: "psql" in command and command[-1].startswith("SHOW "),
        CommandResult(returncode=0, stdout="15.17\n", stderr=""),
    )
    runner.add_response(
        lambda command: "alembic_version" in command[-1],
        CommandResult(returncode=0, stdout="20260305_baseline\n", stderr=""),
    )
    runner.add_response(
        lambda command: "pg_control_system" in command[-1],
        CommandResult(
            returncode=0,
            stdout="7000000000000000001\tknowhere_benchmark\t16384\t4096\n",
            stderr="",
        ),
    )
    runner.add_response(
        lambda command: "FROM pg_indexes" in command[-1],
        CommandResult(
            returncode=0,
            stdout=(
                "public\tdocuments\tix_documents\t8192\n"
                "public\tdocument_chunks\tix_document_chunks\t16384\n"
            ),
            stderr="",
        ),
    )
    return runner


def test_clone_manager_records_full_source_and_database_identity(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    runner = build_fake_runner()
    manager = PostgresCloneManager(layout=layout, runner=runner)

    record = manager.create(run_id=RUN_ID, profile="production")

    assert record.state == "ready"
    assert record.profile == "production"
    assert record.source.volume_name == SOURCE_VOLUME_NAME
    assert record.source.container_name == SOURCE_CONTAINER_NAME
    assert record.source.database_name == SOURCE_DATABASE_NAME
    assert record.source.database_url_port == SOURCE_DATABASE_PORT
    assert record.source.digest.startswith("sha256:")
    assert record.schema_revision == "20260305_baseline"
    assert record.postgres_version == "15.17"
    assert record.server_settings
    assert record.index_inventory
    assert record.database_identity["system_identifier"]
    assert record.data_volume == clone_data_volume_name(RUN_ID)
    assert record.data_volume != record.source.volume_name
    assert record.deviations
    clone_start = next(
        command for command in runner.commands if command[:3] == ("docker", "run", "-d")
    )
    assert clone_start[clone_start.index("--shm-size") + 1] == "2g"

    stored_record = read_clone_record(layout.clone_record_path(RUN_ID))
    assert stored_record == record
    database_url_file = layout.database_url_path(RUN_ID)
    assert database_url_file.is_file()
    assert oct(database_url_file.stat().st_mode)[-3:] == "600"

    source_mutations = [
        command
        for command in runner.commands
        if SOURCE_VOLUME_NAME in command and ":ro" not in " ".join(command)
    ]
    assert [command for command in source_mutations if "-v" in command] == []


def test_clone_manager_restores_source_with_a_read_only_dump(tmp_path: Path) -> None:
    layout = benchmark_layout(tmp_path)
    runner = build_fake_runner()
    manager = PostgresCloneManager(layout=layout, runner=runner)

    manager.create(run_id=RUN_ID, profile="conservative")

    assert len(runner.piped) == 1
    source_command, target_command = runner.piped[0]
    assert "pg_dump" in source_command
    assert SOURCE_CONTAINER_NAME in source_command
    assert "pg_restore" in target_command
    assert "--no-owner" in target_command


def test_failed_restore_keeps_postgres_diagnostics_before_cleanup(
    tmp_path: Path,
) -> None:
    class FailedRestoreRunner(FakeCommandRunner):
        def pipe(
            self,
            source_command: Sequence[str],
            target_command: Sequence[str],
        ) -> None:
            super().pipe(source_command, target_command)
            raise CloneCommandError("restore target closed")

    layout = benchmark_layout(tmp_path)
    runner = FailedRestoreRunner()
    runner.responses = build_fake_runner().responses
    runner.add_response(
        lambda command: tuple(command[:2]) == ("docker", "logs"),
        CommandResult(
            returncode=0, stdout="", stderr="database system is shutting down\n"
        ),
    )
    manager = PostgresCloneManager(layout=layout, runner=runner)

    with pytest.raises(CloneCommandError, match="restore target closed"):
        manager.create(run_id=RUN_ID, profile="conservative")

    assert "database system is shutting down" in layout.postgres_log_path(
        RUN_ID
    ).read_text(encoding="utf-8")
    assert not layout.clone_record_path(RUN_ID).exists()
    assert not layout.database_url_path(RUN_ID).exists()
    log_position = next(
        index
        for index, command in enumerate(runner.commands)
        if command[:2] == ("docker", "logs")
    )
    remove_position = next(
        index
        for index, command in enumerate(runner.commands)
        if command[:3] == ("docker", "volume", "rm")
    )
    assert log_position < remove_position


def test_clone_manager_settles_the_restored_database_before_ready(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    runner = build_fake_runner()
    manager = PostgresCloneManager(layout=layout, runner=runner)

    record = manager.create(run_id=RUN_ID, profile="production")

    settle_commands = [
        command for command in runner.commands if "VACUUM (ANALYZE)" in command
    ]
    assert settle_commands
    assert "restore_maintenance_seconds" in record.database_identity
    assert any("VACUUM (ANALYZE)" in deviation for deviation in record.deviations)


def test_stopped_pgdata_inspection_has_a_bounded_real_runner_path(
    tmp_path: Path,
) -> None:
    class TimedOutRunner(FakeCommandRunner):
        def run_with_timeout(
            self,
            command: Sequence[str],
            *,
            timeout: float,
            check: bool = True,
        ) -> CommandResult:
            self.commands.append(tuple(command))
            assert timeout == 300.0
            raise CloneCommandError("command timed out after 300.0s")

    layout = benchmark_layout(tmp_path)
    manager = PostgresCloneManager(layout=layout, runner=TimedOutRunner())

    with pytest.raises(CloneCommandError, match="timed out after 300.0s"):
        manager._inspect_stopped_pgdata("bounded-template-volume")


def test_clone_manager_reset_and_destroy_never_touch_the_source_volume(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    runner = build_fake_runner()
    manager = PostgresCloneManager(layout=layout, runner=runner)
    manager.create(run_id=RUN_ID, profile="production")

    reset_record = manager.reset(run_id=RUN_ID)
    destroy_result = manager.destroy(run_id=RUN_ID)

    assert reset_record.state == "reset"
    assert destroy_result["state"] == "destroyed"
    assert destroy_result["source_volume_untouched"] == SOURCE_VOLUME_NAME
    removed_volumes = [
        command[-1]
        for command in runner.commands
        if command[:3] == ("docker", "volume", "rm")
    ]
    assert removed_volumes
    assert SOURCE_VOLUME_NAME not in removed_volumes
    assert set(removed_volumes) == {clone_data_volume_name(RUN_ID)}
    final_record = read_clone_record(layout.clone_record_path(RUN_ID))
    assert final_record.state == "destroyed"


def test_clone_manager_refuses_to_reuse_the_source_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.publication_benchmark.clone_db as clone_db_module

    layout = benchmark_layout(tmp_path)
    runner = build_fake_runner()
    manager = PostgresCloneManager(layout=layout, runner=runner)
    monkeypatch.setattr(
        clone_db_module,
        "clone_data_volume_name",
        lambda run_id: SOURCE_VOLUME_NAME,
    )

    with pytest.raises(BenchmarkGuardError, match="immutable source volume"):
        manager.create(run_id=RUN_ID, profile="production")

    assert runner.commands_containing("volume") == []


def test_clone_record_requires_recorded_identity() -> None:
    base = build_clone_record(
        run_id="record-contract",
        database_url="postgresql+psycopg2://postgres:secret@127.0.0.1:55450/db",
    )
    from dataclasses import replace

    with pytest.raises(CloneRecordError, match="source volume digest"):
        replace(
            base,
            source=SourceVolumeIdentity(
                volume_name=SOURCE_VOLUME_NAME,
                digest="",
                container_name=SOURCE_CONTAINER_NAME,
                database_name=SOURCE_DATABASE_NAME,
                database_url_host="127.0.0.1",
                database_url_port=SOURCE_DATABASE_PORT,
            ),
        )
    with pytest.raises(CloneRecordError, match="index inventory"):
        replace(base, index_inventory=())
    with pytest.raises(CloneRecordError, match="database identity"):
        replace(base, database_identity={"system_identifier": "x"})
    with pytest.raises(CloneRecordError, match="recorded PostgreSQL settings"):
        replace(base, server_settings={})
    with pytest.raises(CloneRecordError, match="schema revision"):
        replace(base, schema_revision="")
    with pytest.raises(CloneRecordError, match="profile"):
        replace(base, profile="turbo")


def test_find_listed_clone_refuses_unlisted_mismatched_and_unusable_clones(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    database_url = "postgresql+psycopg2://postgres:secret@127.0.0.1:55450/db"

    with pytest.raises(BenchmarkGuardError, match="unlisted clone"):
        find_listed_clone(
            clones_root=layout.clones_root,
            run_id="missing-run",
            database_url=database_url,
        )

    write_listed_clone(layout, run_id="clone-a", database_url=database_url)
    assert (
        find_listed_clone(
            clones_root=layout.clones_root,
            run_id="clone-a",
            database_url=database_url,
        ).state
        == "ready"
    )

    with pytest.raises(BenchmarkGuardError, match="does not match the listed clone"):
        find_listed_clone(
            clones_root=layout.clones_root,
            run_id="clone-a",
            database_url="postgresql+psycopg2://postgres:other@127.0.0.1:55451/db",
        )

    write_listed_clone(
        layout,
        run_id="clone-b",
        database_url=database_url,
        state="destroyed",
    )
    with pytest.raises(BenchmarkGuardError, match="not listed as usable"):
        find_listed_clone(
            clones_root=layout.clones_root,
            run_id="clone-b",
            database_url=database_url,
        )


def test_guards_refuse_read_only_and_source_database_targets(
    tmp_path: Path,
) -> None:
    source = build_clone_record(
        run_id="guard-contract",
        database_url="postgresql+psycopg2://postgres:secret@127.0.0.1:55450/db",
    ).source

    with pytest.raises(BenchmarkGuardError, match="read-only"):
        assert_writable_database_url(
            "postgresql+psycopg2://knowhere_readonly:secret@127.0.0.1:5432/db",
            purpose="contract",
        )
    with pytest.raises(BenchmarkGuardError, match="read-only"):
        assert_writable_database_url(
            "postgresql+psycopg2://postgres:secret@127.0.0.1:5432/db"
            "?options=-c%20default_transaction_read_only%3Don",
            purpose="contract",
        )
    with pytest.raises(BenchmarkGuardError, match="read-only"):
        assert_writable_database_url(
            "postgresql+psycopg2://postgres:secret@127.0.0.1:5432/db"
            "?target_session_attrs=read-only",
            purpose="contract",
        )
    assert_writable_database_url(
        "postgresql+psycopg2://postgres:secret@127.0.0.1:5432/db"
        "?target_session_attrs=any",
        purpose="contract",
    )
    with pytest.raises(BenchmarkGuardError, match="source database"):
        assert_not_source_database(
            f"postgresql+psycopg2://postgres:secret@127.0.0.1:"
            f"{SOURCE_DATABASE_PORT}/{SOURCE_DATABASE_NAME}",
            source=source,
            purpose="contract",
        )

    url_file = tmp_path / "database-url"
    url_file.write_text(
        "postgresql+psycopg2://knowhere_readonly:secret@127.0.0.1:5432/db\n",
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkGuardError, match="read-only"):
        read_database_url_file(url_file, purpose="contract")

    url_file.write_text("", encoding="utf-8")
    with pytest.raises(BenchmarkGuardError, match="exactly one line"):
        read_database_url_file(url_file, purpose="contract")


def test_clone_record_round_trip_preserves_identity(tmp_path: Path) -> None:
    layout = benchmark_layout(tmp_path)
    record = write_listed_clone(
        layout,
        run_id="round-trip",
        database_url="postgresql+psycopg2://postgres:secret@127.0.0.1:55450/db",
    )

    loaded: CloneRecord = read_clone_record(layout.clone_record_path("round-trip"))

    assert loaded == record
    assert loaded.settings_digest().startswith("sha256:")
