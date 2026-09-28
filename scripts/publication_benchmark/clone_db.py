"""Create, query, reset, and destroy writable benchmark database clones.

The September production dump stays immutable in its stable Docker named
volume. Every clone is restored from that source into its own named volume and
container, and the clone record captures the source volume digest, schema
revision, PostgreSQL settings, index inventory, and database identity.

    uv run python scripts/publication_benchmark/clone_db.py \
      --source-volume knowhere-prod-restore-data \
      --run-id <run-id> \
      --postgres-config production
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.clone_state import (  # noqa: E402
    CloneRecord,
    SourceVolumeIdentity,
    clone_container_name,
    clone_data_volume_name,
    read_clone_record,
    write_clone_record,
)
from scripts.publication_benchmark.guards import (  # noqa: E402
    BenchmarkGuardError,
    assert_clone_target_is_distinct_from_source,
    database_url_digest,
)
from scripts.publication_benchmark.layout import (  # noqa: E402
    BenchmarkLayout,
    validate_run_id,
)

DEFAULT_SOURCE_VOLUME: str = "knowhere-prod-restore-data"
DEFAULT_SOURCE_CONTAINER: str = "knowhere-prod-restore-pg"
DEFAULT_SOURCE_DATABASE: str = "knowhere"
DEFAULT_CLONE_DATABASE: str = "knowhere_benchmark"
POSTGRES_IMAGE: str = "postgres:15-alpine"
CLONE_SHARED_MEMORY_SIZE: str = "2g"
# The helper container runs BusyBox utilities from the same image the clone
# uses, so clone creation needs no registry access on a machine that already
# holds the immutable source volume.
HELPER_IMAGE: str = POSTGRES_IMAGE
CLONE_DATABASE_USER: str = "postgres"
CLONE_RESTORE_JOBS: int = 1
CLONE_DUMP_COMPRESSION_LEVEL: int = 1
CLONE_DUMP_FILE_IN_CONTAINER: str = "/tmp/bench-clone.dump"
SETTLE_RESTORED_DATABASE_SQL: str = "VACUUM (ANALYZE)"
CLONE_PORT_BASE: int = 55432
CLONE_PORT_SPAN: int = 1000
READY_TIMEOUT_SECONDS: float = 120.0
READY_POLL_SECONDS: float = 1.0
STOPPED_PGDATA_INSPECTION_TIMEOUT_SECONDS: float = 300.0
TEMPLATE_SCHEMA_VERSION: str = "publication-clone-template/1"
TEMPLATE_DIRECTORY_NAME: str = "templates"
TEMPLATE_RECORD_FILE_NAME: str = "template.json"
TEMPLATE_DATABASE_URL_FILE_NAME: str = "database-url"

RECORDED_SETTINGS: tuple[str, ...] = (
    "max_connections",
    "shared_buffers",
    "work_mem",
    "maintenance_work_mem",
    "effective_cache_size",
    "random_page_cost",
    "wal_compression",
    "checkpoint_completion_target",
    "synchronous_commit",
    "full_page_writes",
    "max_wal_size",
    "min_wal_size",
    "temp_buffers",
)

PRODUCTION_PROFILE_SETTINGS: Mapping[str, str] = {
    "max_connections": "200",
    "shared_buffers": "2GB",
    "work_mem": "16MB",
    "maintenance_work_mem": "512MB",
    "effective_cache_size": "6GB",
    "random_page_cost": "1.1",
    "wal_compression": "on",
    "checkpoint_completion_target": "0.9",
    "synchronous_commit": "on",
    "full_page_writes": "on",
    "max_wal_size": "8GB",
    "min_wal_size": "1GB",
}

PRODUCTION_PROFILE_DEVIATIONS: tuple[str, ...] = (
    "shared_buffers is applied only when the local container can reserve 2GB; "
    "the effective value is recorded under server_settings",
    "effective_cache_size is advisory and reflects the local machine, not the "
    "production instance memory",
    "max_connections is recorded but the benchmark runs one client per sample",
    "the restored clone is settled with VACUUM (ANALYZE) before it is marked "
    "ready so a sample does not race the restore's own autovacuum",
)

CONSERVATIVE_PROFILE_DEVIATIONS: tuple[str, ...] = (
    "conservative profile keeps the local dump-container settings to expose "
    "I/O sensitivity instead of matching production",
)


class CloneCommandError(RuntimeError):
    """Raised when a Docker or PostgreSQL clone command fails."""


@dataclass(frozen=True)
class CommandResult:
    """Captured output of one external command."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Executes external commands for the clone manager."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        stdin: bytes | None = None,
        check: bool = True,
    ) -> CommandResult: ...

    def pipe(
        self, source_command: Sequence[str], target_command: Sequence[str]
    ) -> None:
        """Pipe one command's stdout into another command's stdin."""
        ...


class SubprocessCommandRunner:
    """Default command runner backed by ``subprocess``."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        stdin: bytes | None = None,
        check: bool = True,
    ) -> CommandResult:
        completed = subprocess.run(
            list(command),
            input=stdin,
            capture_output=True,
            check=False,
        )
        result = CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout.decode("utf-8", errors="replace"),
            stderr=completed.stderr.decode("utf-8", errors="replace"),
        )
        if check and result.returncode != 0:
            raise CloneCommandError(
                f"command failed ({result.returncode}): {' '.join(command)}\n"
                f"{result.stderr.strip()}"
            )
        return result

    def run_with_timeout(
        self,
        command: Sequence[str],
        *,
        timeout: float,
        check: bool = True,
    ) -> CommandResult:
        """Run one command with a bounded wall-clock duration."""
        try:
            completed = subprocess.run(
                list(command),
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise CloneCommandError(
                f"command timed out after {timeout:.1f}s: {' '.join(command)}"
            ) from error
        result = CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout.decode("utf-8", errors="replace")
            if isinstance(completed.stdout, bytes)
            else (completed.stdout or ""),
            stderr=completed.stderr.decode("utf-8", errors="replace")
            if isinstance(completed.stderr, bytes)
            else (completed.stderr or ""),
        )
        if check and result.returncode != 0:
            raise CloneCommandError(
                f"command failed ({result.returncode}): {' '.join(command)}\n"
                f"{result.stderr.strip()}"
            )
        return result

    def pipe(
        self,
        source_command: Sequence[str],
        target_command: Sequence[str],
    ) -> None:
        source = subprocess.Popen(list(source_command), stdout=subprocess.PIPE)
        target = subprocess.Popen(
            list(target_command),
            stdin=source.stdout,
        )
        assert source.stdout is not None
        source.stdout.close()
        target_returncode = target.wait()
        source_returncode = source.wait()
        if source_returncode != 0 or target_returncode != 0:
            raise CloneCommandError(
                "pipe failed: "
                f"{' '.join(source_command)} -> {' '.join(target_command)} "
                f"(source={source_returncode}, target={target_returncode})"
            )


def clone_port_for_run(run_id: str) -> int:
    """Return the deterministic host port reserved for one clone."""
    digest = sha256(validate_run_id(run_id).encode("utf-8")).hexdigest()
    return CLONE_PORT_BASE + (int(digest[:8], 16) % CLONE_PORT_SPAN)


class PostgresCloneManager:
    """Create and manage writable benchmark clones."""

    def __init__(
        self,
        *,
        layout: BenchmarkLayout,
        source_volume: str = DEFAULT_SOURCE_VOLUME,
        source_container: str = DEFAULT_SOURCE_CONTAINER,
        source_database: str = DEFAULT_SOURCE_DATABASE,
        runner: CommandRunner | None = None,
    ) -> None:
        self._layout = layout
        self._source_volume = source_volume
        self._source_container = source_container
        self._source_database = source_database
        self._runner: CommandRunner = runner or SubprocessCommandRunner()

    # -- source identity -------------------------------------------------

    def source_volume_identity(self) -> SourceVolumeIdentity:
        """Record the immutable source volume identity and digest."""
        inventory = self._runner(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{self._source_volume}:/from:ro",
                HELPER_IMAGE,
                "sh",
                "-c",
                "find /from -maxdepth 3 -type f -exec stat -c '%n %s' {} + "
                "| sort | sha256sum; sha256sum /from/PG_VERSION "
                "/from/global/pg_control 2>/dev/null || true",
            ]
        )
        digest = "sha256:" + sha256(inventory.stdout.encode("utf-8")).hexdigest()
        self._runner(
            ["docker", "container", "inspect", self._source_container],
        )
        source_port = self._published_port(self._source_container)
        return SourceVolumeIdentity(
            volume_name=self._source_volume,
            digest=digest,
            container_name=self._source_container,
            database_name=self._source_database,
            database_url_host="127.0.0.1",
            database_url_port=source_port,
        )

    def _published_port(self, container_name: str) -> int:
        result = self._runner(
            [
                "docker",
                "inspect",
                container_name,
                "--format",
                "{{json .NetworkSettings.Ports}}",
            ]
        )
        raw = result.stdout.strip()
        if raw.isdigit():
            return int(raw)
        try:
            published = json.loads(raw or "{}")
        except json.JSONDecodeError as error:
            raise CloneCommandError(
                f"could not read the published port of {container_name}: {raw!r}"
            ) from error
        if not isinstance(published, dict):
            raise CloneCommandError(
                f"unexpected port payload for {container_name}: {raw!r}"
            )
        # A container publishes the same host port once per address family, so
        # the bindings must be read individually instead of concatenated.
        for bindings in published.values():
            if not isinstance(bindings, list):
                continue
            for binding in bindings:
                if not isinstance(binding, dict):
                    continue
                host_port = str(binding.get("HostPort") or "")
                if host_port.isdigit():
                    return int(host_port)
        return 0

    # -- lifecycle -------------------------------------------------------

    def create(
        self,
        *,
        run_id: str,
        profile: str,
        template_id: str | None = None,
    ) -> CloneRecord:
        """Create one writable clone from the immutable source volume."""
        validate_run_id(run_id)
        if profile not in ("production", "conservative"):
            raise CloneCommandError(f"unsupported PostgreSQL profile {profile!r}")
        if template_id is not None:
            return self._create_from_template(
                run_id=run_id,
                profile=profile,
                template_id=template_id,
            )
        source = self.source_volume_identity()
        clone_volume = clone_data_volume_name(run_id)
        clone_container = clone_container_name(run_id)
        assert_clone_target_is_distinct_from_source(
            source_volume_name=source.volume_name,
            clone_volume_name=clone_volume,
            clone_container_name=clone_container,
            purpose="clone creation",
        )
        self._assert_target_absent(
            clone_volume=clone_volume,
            clone_container=clone_container,
        )

        self._layout.ensure_clone_directory(run_id)
        port = clone_port_for_run(run_id)
        password = secrets.token_hex(16)
        self._runner(["docker", "volume", "create", clone_volume])
        try:
            self._start_clone_container(
                profile=profile,
                clone_volume=clone_volume,
                clone_container=clone_container,
                port=port,
                password=password,
            )
            self._wait_until_ready(clone_container)
            self._restore_source_database(clone_container)
            settle_seconds = self._settle_restored_database(clone_container)
            record = self._build_record(
                run_id=run_id,
                profile=profile,
                source=source,
                clone_volume=clone_volume,
                clone_container=clone_container,
                port=port,
                password=password,
                settle_seconds=settle_seconds,
            )
            self._capture_postgres_log(clone_container, run_id=run_id)
            write_clone_record(self._layout.clone_record_path(run_id), record)
        except Exception:
            # A half-restored clone is never evidence: drop it and surface the
            # failure instead of leaving resources that a later run could reuse.
            # Keep PostgreSQL's last log before deleting the failed container;
            # restore failures otherwise lose the only server-side diagnosis.
            try:
                self._capture_postgres_log(clone_container, run_id=run_id)
            except Exception:
                pass
            try:
                self._delete_clone_resources(
                    clone_volume=clone_volume,
                    clone_container=clone_container,
                )
            finally:
                self._layout.clone_record_path(run_id).unlink(missing_ok=True)
                self._layout.database_url_path(run_id).unlink(missing_ok=True)
            raise
        return record

    def _template_directory(self, template_id: str) -> Path:
        return (
            self._layout.publication_root
            / TEMPLATE_DIRECTORY_NAME
            / validate_run_id(template_id)
        )

    def _template_record_path(self, template_id: str) -> Path:
        return self._template_directory(template_id) / TEMPLATE_RECORD_FILE_NAME

    def _template_database_url_path(self, template_id: str) -> Path:
        return self._template_directory(template_id) / TEMPLATE_DATABASE_URL_FILE_NAME

    def _inspect_running(self, container_name: str) -> bool:
        result = self._runner(
            [
                "docker",
                "inspect",
                container_name,
                "--format",
                "{{.State.Running}}",
            ]
        )
        state = result.stdout.strip().lower()
        if state not in ("true", "false"):
            raise CloneCommandError("cannot determine template container state")
        return state == "true"

    def _inspect_stopped_pgdata(self, volume_name: str) -> str:
        """Refuse a partial cluster, external storage, or an unclean shutdown."""
        command = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume_name}:/from:ro",
            HELPER_IMAGE,
            "sh",
            "-eu",
            "-c",
            "test -f /from/PG_VERSION; "
            "test ! -L /from/pg_wal; "
            'test -z "$(find /from/pg_tblspc -mindepth 1 -maxdepth 1 '
            '-print -quit)"; '
            "test ! -e /from/postmaster.pid; "
            "cat /from/PG_VERSION; pg_controldata /from",
        ]
        bounded_runner = getattr(self._runner, "run_with_timeout", None)
        if bounded_runner is None:
            # Contract-test runners intentionally expose only CommandRunner.
            result = self._runner(command)
        else:
            result = bounded_runner(
                command,
                timeout=STOPPED_PGDATA_INSPECTION_TIMEOUT_SECONDS,
            )
        lines = result.stdout.splitlines()
        if not lines or lines[0].strip() != "15":
            raise CloneCommandError(
                "template requires a complete PostgreSQL 15 cluster"
            )
        states = [
            line.partition(":")[2].strip().lower()
            for line in lines
            if line.lower().startswith("database cluster state:")
        ]
        if states != ["shut down"]:
            raise CloneCommandError("template PostgreSQL cluster is not shut down")
        return result.stdout

    def seal_template(self, *, run_id: str) -> dict[str, object]:
        """Stop a never-sampled restored clone and seal its volume as a template."""
        validate_run_id(run_id)
        record_path = self._layout.clone_record_path(run_id)
        record = read_clone_record(record_path)
        if (
            record.data_volume != clone_data_volume_name(run_id)
            or record.container_name != clone_container_name(run_id)
            or record.source.volume_name != self._source_volume
            or record.source.container_name != self._source_container
            or record.container_name == self._source_container
        ):
            raise CloneCommandError("template source clone identity is unsafe")
        if record.state != "ready":
            raise CloneCommandError("only an unused ready clone can become a template")
        if self._layout.publication_record_path(run_id).exists():
            raise CloneCommandError("a sampled clone cannot become a template")
        template_path = self._template_record_path(run_id)
        if template_path.parent.exists():
            raise CloneCommandError("template already exists")
        assert_clone_target_is_distinct_from_source(
            source_volume_name=record.source.volume_name,
            clone_volume_name=record.data_volume,
            clone_container_name=record.container_name,
            purpose="template sealing",
        )
        database_url_path = self._layout.database_url_path(run_id)
        if not database_url_path.is_file():
            raise CloneCommandError("template database URL file is missing")
        database_url = database_url_path.read_text(encoding="utf-8").strip()
        if database_url_digest(database_url) != record.database_url_digest:
            raise CloneCommandError("template database URL does not match its record")
        database_location = make_url(database_url)
        if (
            database_location.host != "127.0.0.1"
            or database_location.port != record.port
            or database_location.database != record.database_name
        ):
            raise CloneCommandError("template database URL points outside its clone")
        active_sessions = self._psql(
            record.container_name,
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()",
        )
        if not active_sessions or int(active_sessions[0][0]) != 0:
            raise CloneCommandError("template clone has other active database sessions")
        self._runner(["docker", "stop", "-t", "120", record.container_name])
        if self._inspect_running(record.container_name):
            raise CloneCommandError("template container did not stop")
        control_output = self._inspect_stopped_pgdata(record.data_volume)
        digest_result = self._runner(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{record.data_volume}:/from:ro",
                HELPER_IMAGE,
                "sh",
                "-eu",
                "-c",
                "set -o pipefail; tar -C /from -cf - . | sha256sum",
            ]
        )
        content_digest = (
            digest_result.stdout.split()[0] if digest_result.stdout.split() else ""
        )
        if len(content_digest) != 64 or any(
            character not in "0123456789abcdef" for character in content_digest
        ):
            raise CloneCommandError("could not fingerprint the stopped template")
        template_path.parent.mkdir(parents=True, exist_ok=False)
        # The directory protects this volume from reset/destroy even if sealing
        # is interrupted before template.json is written.
        write_clone_record(record_path, record.with_state("destroyed"))
        template_url_path = self._template_database_url_path(run_id)
        template_url_path.write_bytes(database_url_path.read_bytes())
        template_url_path.chmod(0o600)
        template_payload: dict[str, object] = {
            "schema_version": TEMPLATE_SCHEMA_VERSION,
            "template_id": run_id,
            "state": "sealed",
            "sealed_at": cli_support.utc_now_iso(),
            "content_digest": f"sha256:{content_digest}",
            "control_digest": "sha256:"
            + sha256(control_output.encode("utf-8")).hexdigest(),
            "source_clone": record.to_dict(),
        }
        cli_support.write_json(template_path, template_payload)
        record_path.unlink()
        database_url_path.unlink()
        return {
            "template_id": run_id,
            "volume_name": record.data_volume,
            "profile": record.profile,
            "content_digest": template_payload["content_digest"],
            "state": "sealed",
        }

    def _read_sealed_template(
        self,
        *,
        template_id: str,
        profile: str,
    ) -> tuple[CloneRecord, str, str]:
        template_path = self._template_record_path(template_id)
        if not template_path.is_file():
            raise CloneCommandError("sealed template record is missing")
        try:
            payload = json.loads(template_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("template payload is not an object")
            record = CloneRecord.from_dict(payload["source_clone"])
        except (KeyError, ValueError, TypeError) as error:
            raise CloneCommandError("sealed template record is invalid") from error
        if (
            payload.get("schema_version") != TEMPLATE_SCHEMA_VERSION
            or payload.get("template_id") != template_id
            or payload.get("state") != "sealed"
        ):
            raise CloneCommandError("sealed template record is incompatible")
        content_digest = str(payload.get("content_digest") or "")
        digest_hex = content_digest.removeprefix("sha256:")
        if len(digest_hex) != 64 or any(
            character not in "0123456789abcdef" for character in digest_hex
        ):
            raise CloneCommandError("sealed template content digest is missing")
        if record.run_id != template_id or record.profile != profile:
            raise CloneCommandError(
                "sealed template identity or profile does not match"
            )
        if (
            record.source.volume_name != self._source_volume
            or record.source.container_name != self._source_container
            or record.data_volume != clone_data_volume_name(template_id)
            or record.container_name != clone_container_name(template_id)
            or record.container_name == self._source_container
            or record.postgres_image != POSTGRES_IMAGE
            or not record.postgres_version.startswith("15.")
            or record.state != "ready"
        ):
            raise CloneCommandError("sealed template source volume does not match")
        if self._inspect_running(record.container_name):
            raise CloneCommandError("sealed template container is running")
        control_output = self._inspect_stopped_pgdata(record.data_volume)
        control_digest = "sha256:" + sha256(control_output.encode("utf-8")).hexdigest()
        if control_digest != payload.get("control_digest"):
            raise CloneCommandError("sealed template control metadata changed")
        url_path = self._template_database_url_path(template_id)
        if not url_path.is_file():
            raise CloneCommandError("sealed template database URL file is missing")
        try:
            database_url = url_path.read_text(encoding="utf-8").strip()
            url = make_url(database_url)
        except ValueError as error:
            raise CloneCommandError(
                "sealed template database URL is invalid"
            ) from error
        if database_url_digest(database_url) != record.database_url_digest:
            raise CloneCommandError(
                "sealed template database URL does not match its record"
            )
        if (
            url.host != "127.0.0.1"
            or url.port != record.port
            or url.database != record.database_name
        ):
            raise CloneCommandError(
                "sealed template database URL points outside its clone"
            )
        password = url.password
        if not password:
            raise CloneCommandError("sealed template database password is missing")
        return record, content_digest, password

    def _create_from_template(
        self,
        *,
        run_id: str,
        profile: str,
        template_id: str,
    ) -> CloneRecord:
        template_id = validate_run_id(template_id)
        template, content_digest, password = self._read_sealed_template(
            template_id=template_id,
            profile=profile,
        )
        clone_volume = clone_data_volume_name(run_id)
        clone_container = clone_container_name(run_id)
        assert_clone_target_is_distinct_from_source(
            source_volume_name=template.source.volume_name,
            clone_volume_name=clone_volume,
            clone_container_name=clone_container,
            purpose="template clone creation",
        )
        if (
            clone_volume == template.data_volume
            or clone_container == template.container_name
        ):
            raise CloneCommandError("template and sample clone identities must differ")
        self._assert_target_absent(
            clone_volume=clone_volume,
            clone_container=clone_container,
        )
        self._layout.ensure_clone_directory(run_id)
        port = clone_port_for_run(run_id)
        self._runner(["docker", "volume", "create", clone_volume])
        try:
            self._runner(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{template.data_volume}:/from:ro",
                    "-v",
                    f"{clone_volume}:/to",
                    HELPER_IMAGE,
                    "sh",
                    "-eu",
                    "-c",
                    'test -z "$(find /to -mindepth 1 -print -quit)"; '
                    "cp -a /from/. /to/; sync",
                ]
            )
            self._start_clone_container(
                profile=profile,
                clone_volume=clone_volume,
                clone_container=clone_container,
                port=port,
                password=password,
            )
            self._wait_until_ready(clone_container)
            record = self._build_record(
                run_id=run_id,
                profile=profile,
                source=template.source,
                clone_volume=clone_volume,
                clone_container=clone_container,
                port=port,
                password=password,
                settle_seconds=0.0,
            )
            if (
                record.schema_revision != template.schema_revision
                or record.postgres_version != template.postgres_version
                or record.server_settings != template.server_settings
                or record.index_inventory != template.index_inventory
            ):
                raise CloneCommandError(
                    "template clone metadata differs from the sealed template"
                )
            record = replace(
                record,
                database_identity={
                    **record.database_identity,
                    "template_id": template_id,
                    "template_content_digest": content_digest,
                },
            )
            self._capture_postgres_log(clone_container, run_id=run_id)
            write_clone_record(self._layout.clone_record_path(run_id), record)
        except Exception:
            try:
                self._capture_postgres_log(clone_container, run_id=run_id)
            except Exception:
                pass
            try:
                self._delete_clone_resources(
                    clone_volume=clone_volume,
                    clone_container=clone_container,
                )
            finally:
                self._layout.clone_record_path(run_id).unlink(missing_ok=True)
                self._layout.database_url_path(run_id).unlink(missing_ok=True)
            raise
        return record

    def _assert_target_absent(
        self,
        *,
        clone_volume: str,
        clone_container: str,
    ) -> None:
        for command in (
            ["docker", "volume", "inspect", clone_volume],
            ["docker", "container", "inspect", clone_container],
        ):
            result = self._runner(command, check=False)
            if result.returncode == 0:
                raise CloneCommandError("clone target already exists")
            error_text = result.stderr.lower()
            if not any(marker in error_text for marker in ("no such", "not found")):
                raise CloneCommandError("cannot verify that clone target is absent")

    def _start_clone_container(
        self,
        *,
        profile: str,
        clone_volume: str,
        clone_container: str,
        port: int,
        password: str,
    ) -> None:
        command = [
            "docker",
            "run",
            "-d",
            "--shm-size",
            CLONE_SHARED_MEMORY_SIZE,
            "--name",
            clone_container,
            "-v",
            f"{clone_volume}:/var/lib/postgresql/data",
            "-p",
            f"127.0.0.1:{port}:5432",
            "-e",
            f"POSTGRES_DB={DEFAULT_CLONE_DATABASE}",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            POSTGRES_IMAGE,
        ]
        if profile == "production":
            for key, value in sorted(PRODUCTION_PROFILE_SETTINGS.items()):
                command.extend(["-c", f"{key}={value}"])
        try:
            self._runner(command)
        except CloneCommandError:
            # The Docker command carries POSTGRES_PASSWORD; never surface it.
            raise CloneCommandError(
                f"could not start clone container {clone_container}"
            ) from None

    def _wait_until_ready(self, clone_container: str) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            result = self._runner(
                [
                    "docker",
                    "exec",
                    clone_container,
                    "pg_isready",
                    "-U",
                    CLONE_DATABASE_USER,
                    "-d",
                    DEFAULT_CLONE_DATABASE,
                ],
                check=False,
            )
            if result.returncode == 0:
                return
            time.sleep(READY_POLL_SECONDS)
        raise CloneCommandError(
            f"clone container {clone_container} did not become ready within "
            f"{READY_TIMEOUT_SECONDS}s"
        )

    def _settle_restored_database(self, clone_container: str) -> float:
        """Vacuum and analyze the restored database before it becomes a sample.

        A logical restore inserts every row, which immediately exceeds the
        autovacuum thresholds. Settling the clone here keeps that maintenance
        work out of the timed publication window.
        """
        started_at = time.monotonic()
        self._runner(
            [
                "docker",
                "exec",
                clone_container,
                "psql",
                "-U",
                CLONE_DATABASE_USER,
                "-d",
                DEFAULT_CLONE_DATABASE,
                "-c",
                SETTLE_RESTORED_DATABASE_SQL,
            ]
        )
        return round(time.monotonic() - started_at, 3)

    def _restore_source_database(self, clone_container: str) -> None:
        dump_command = [
            "docker",
            "exec",
            self._source_container,
            "pg_dump",
            "-U",
            CLONE_DATABASE_USER,
            "--no-owner",
            "--no-acl",
            "-Z",
            str(CLONE_DUMP_COMPRESSION_LEVEL),
            "-Fc",
            self._source_database,
        ]
        if CLONE_RESTORE_JOBS > 1:
            # pg_restore can only parallelize from a seekable file, so stage the
            # dump inside the clone container and delete it afterwards.
            self._runner.pipe(
                dump_command,
                [
                    "docker",
                    "exec",
                    "-i",
                    clone_container,
                    "sh",
                    "-c",
                    f"cat > {CLONE_DUMP_FILE_IN_CONTAINER}",
                ],
            )
            self._runner(
                [
                    "docker",
                    "exec",
                    clone_container,
                    "pg_restore",
                    "--no-owner",
                    "--no-acl",
                    "--jobs",
                    str(CLONE_RESTORE_JOBS),
                    "-U",
                    CLONE_DATABASE_USER,
                    "-d",
                    DEFAULT_CLONE_DATABASE,
                    CLONE_DUMP_FILE_IN_CONTAINER,
                ]
            )
            self._runner(
                [
                    "docker",
                    "exec",
                    clone_container,
                    "rm",
                    "-f",
                    CLONE_DUMP_FILE_IN_CONTAINER,
                ],
                check=False,
            )
            return
        self._runner.pipe(
            dump_command,
            [
                "docker",
                "exec",
                "-i",
                clone_container,
                "pg_restore",
                "--no-owner",
                "--no-acl",
                "--single-transaction",
                "-U",
                CLONE_DATABASE_USER,
                "-d",
                DEFAULT_CLONE_DATABASE,
            ],
        )

    def _build_record(
        self,
        *,
        run_id: str,
        profile: str,
        source: SourceVolumeIdentity,
        clone_volume: str,
        clone_container: str,
        port: int,
        password: str,
        settle_seconds: float = 0.0,
    ) -> CloneRecord:
        database_url = (
            "postgresql+psycopg2://"
            f"{CLONE_DATABASE_USER}:{password}@127.0.0.1:{port}/"
            f"{DEFAULT_CLONE_DATABASE}"
        )
        self._layout.database_url_path(run_id).write_text(
            database_url + "\n",
            encoding="utf-8",
        )
        self._layout.database_url_path(run_id).chmod(0o600)
        return CloneRecord(
            clone_id=run_id,
            run_id=run_id,
            created_at=cli_support.utc_now_iso(),
            profile=profile,
            postgres_image=POSTGRES_IMAGE,
            postgres_version=self._server_version(clone_container),
            schema_revision=self._schema_revision(clone_container),
            source=source,
            data_volume=clone_volume,
            container_name=clone_container,
            port=port,
            database_name=DEFAULT_CLONE_DATABASE,
            database_url_digest=database_url_digest(database_url),
            database_identity={
                **self._database_identity(clone_container),
                "restore_maintenance_seconds": settle_seconds,
            },
            server_settings=self._server_settings(clone_container),
            index_inventory=self._index_inventory(clone_container),
            deviations=(
                PRODUCTION_PROFILE_DEVIATIONS
                if profile == "production"
                else CONSERVATIVE_PROFILE_DEVIATIONS
            ),
            state="ready",
        )

    def query(self, *, run_id: str) -> dict[str, Any]:
        """Query the clone lifecycle state and connectivity."""
        record = read_clone_record(self._layout.clone_record_path(run_id))
        result = self._runner(
            [
                "docker",
                "exec",
                record.container_name,
                "psql",
                "-U",
                CLONE_DATABASE_USER,
                "-d",
                record.database_name,
                "-At",
                "-c",
                "SELECT 1",
            ],
            check=False,
        )
        return {
            "run_id": run_id,
            "state": record.state,
            "container_name": record.container_name,
            "profile": record.profile,
            "schema_revision": record.schema_revision,
            "reachable": result.returncode == 0,
        }

    def record_existing(self, *, run_id: str, profile: str) -> CloneRecord:
        """Record an already restored clone without repeating the restore.

        Long restores are expensive, so a clone whose container is already
        serving the restored database can be adopted by writing its record from
        the running container instead of dumping the source again.
        """
        validate_run_id(run_id)
        if profile not in ("production", "conservative"):
            raise CloneCommandError(f"unsupported PostgreSQL profile {profile!r}")
        clone_container = clone_container_name(run_id)
        clone_volume = clone_data_volume_name(run_id)
        source = self.source_volume_identity()
        assert_clone_target_is_distinct_from_source(
            source_volume_name=source.volume_name,
            clone_volume_name=clone_volume,
            clone_container_name=clone_container,
            purpose="clone recording",
        )
        database_url_path = self._layout.database_url_path(run_id)
        if not database_url_path.is_file():
            raise CloneCommandError(
                f"cannot record clone {run_id!r}: {database_url_path} is missing, "
                "so the clone password is unknown; destroy the clone and create "
                "it again"
            )
        self._layout.ensure_clone_directory(run_id)
        table_count = self._psql(
            clone_container,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public'",
        )
        if not table_count or int(table_count[0][0]) == 0:
            raise CloneCommandError(
                f"cannot record clone {run_id!r}: {clone_container} has no "
                "restored tables"
            )
        record = CloneRecord(
            clone_id=run_id,
            run_id=run_id,
            created_at=cli_support.utc_now_iso(),
            profile=profile,
            postgres_image=POSTGRES_IMAGE,
            postgres_version=self._server_version(clone_container),
            schema_revision=self._schema_revision(clone_container),
            source=source,
            data_volume=clone_volume,
            container_name=clone_container,
            port=clone_port_for_run(run_id),
            database_name=DEFAULT_CLONE_DATABASE,
            database_url_digest=database_url_digest(
                database_url_path.read_text(encoding="utf-8").strip()
            ),
            database_identity={
                **self._database_identity(clone_container),
                "restore_maintenance_seconds": 0.0,
            },
            server_settings=self._server_settings(clone_container),
            index_inventory=self._index_inventory(clone_container),
            deviations=(
                PRODUCTION_PROFILE_DEVIATIONS
                if profile == "production"
                else CONSERVATIVE_PROFILE_DEVIATIONS
            ),
            state="ready",
        )
        self._capture_postgres_log(clone_container, run_id=run_id)
        write_clone_record(self._layout.clone_record_path(run_id), record)
        return record

    def reset(self, *, run_id: str, template_id: str | None = None) -> CloneRecord:
        """Destroy and recreate one clone so a sample starts from source state."""
        previous = read_clone_record(self._layout.clone_record_path(run_id))
        self._assert_managed_clone_target(previous, run_id=run_id)
        if template_id is not None:
            template, _, _ = self._read_sealed_template(
                template_id=validate_run_id(template_id),
                profile=previous.profile,
            )
            if previous.data_volume == template.data_volume:
                raise CloneCommandError("cannot reset the sealed template volume")
        self._assert_not_template_volume(previous.data_volume)
        write_clone_record(
            self._layout.clone_record_path(run_id),
            previous.with_state("destroyed"),
        )
        try:
            self._delete_clone_resources(
                clone_volume=previous.data_volume,
                clone_container=previous.container_name,
            )
        except Exception:
            write_clone_record(
                self._layout.clone_record_path(run_id),
                previous.with_state("failed"),
            )
            raise
        try:
            record = self.create(
                run_id=run_id,
                profile=previous.profile,
                template_id=template_id,
            )
        except Exception:
            write_clone_record(
                self._layout.clone_record_path(run_id),
                previous.with_state("failed"),
            )
            raise
        reset_record = record.with_state("reset")
        write_clone_record(self._layout.clone_record_path(run_id), reset_record)
        return reset_record

    def destroy(self, *, run_id: str) -> dict[str, Any]:
        """Destroy one clone without touching the source volume."""
        record = read_clone_record(self._layout.clone_record_path(run_id))
        self._assert_managed_clone_target(record, run_id=run_id)
        self._assert_not_template_volume(record.data_volume)
        assert_clone_target_is_distinct_from_source(
            source_volume_name=record.source.volume_name,
            clone_volume_name=record.data_volume,
            clone_container_name=record.container_name,
            purpose="clone destruction",
        )
        try:
            self._delete_clone_resources(
                clone_volume=record.data_volume,
                clone_container=record.container_name,
            )
        except Exception:
            write_clone_record(
                self._layout.clone_record_path(run_id),
                record.with_state("failed"),
            )
            raise
        write_clone_record(
            self._layout.clone_record_path(run_id),
            record.with_state("destroyed"),
        )
        return {
            "run_id": run_id,
            "state": "destroyed",
            "destroyed_volume": record.data_volume,
            "source_volume_untouched": record.source.volume_name,
        }

    def _delete_clone_resources(
        self,
        *,
        clone_volume: str,
        clone_container: str,
    ) -> None:
        if clone_volume == self._source_volume:
            raise CloneCommandError("refusing to delete the September source volume")
        if clone_container == self._source_container:
            raise CloneCommandError("refusing to delete the September source container")
        self._assert_not_template_volume(clone_volume)
        self._runner(
            ["docker", "rm", "-f", clone_container],
            check=False,
        )
        self._runner(["docker", "volume", "rm", clone_volume])

    def _assert_managed_clone_target(
        self,
        record: CloneRecord,
        *,
        run_id: str,
    ) -> None:
        if (
            record.data_volume != clone_data_volume_name(run_id)
            or record.container_name != clone_container_name(run_id)
            or record.data_volume == self._source_volume
            or record.container_name == self._source_container
        ):
            raise CloneCommandError("clone record does not identify its managed target")

    def _assert_not_template_volume(self, volume_name: str) -> None:
        template_root = self._layout.publication_root / TEMPLATE_DIRECTORY_NAME
        if not template_root.is_dir():
            return
        for template_directory in template_root.iterdir():
            if not template_directory.is_dir():
                continue
            try:
                protected_volume = clone_data_volume_name(template_directory.name)
            except ValueError:
                continue
            if protected_volume == volume_name:
                raise CloneCommandError("refusing to delete a sealed template volume")
        for template_path in template_root.glob(f"*/{TEMPLATE_RECORD_FILE_NAME}"):
            try:
                payload = json.loads(template_path.read_text(encoding="utf-8"))
                record = CloneRecord.from_dict(payload["source_clone"])
            except (KeyError, ValueError, TypeError) as error:
                raise CloneCommandError(
                    "cannot verify sealed template volumes"
                ) from error
            if record.data_volume == volume_name:
                raise CloneCommandError("refusing to delete a sealed template volume")

    def _capture_postgres_log(self, clone_container: str, *, run_id: str) -> None:
        """Persist the clone container log next to the clone record."""
        result = self._runner(
            ["docker", "logs", clone_container],
            check=False,
        )
        self._layout.postgres_log_path(run_id).write_text(
            result.stdout + result.stderr,
            encoding="utf-8",
        )

    # -- introspection ---------------------------------------------------

    def _psql(self, clone_container: str, statement: str) -> list[list[str]]:
        result = self._runner(
            [
                "docker",
                "exec",
                clone_container,
                "psql",
                "-U",
                CLONE_DATABASE_USER,
                "-d",
                DEFAULT_CLONE_DATABASE,
                "-At",
                "-F",
                "\t",
                "-c",
                statement,
            ]
        )
        return [line.split("\t") for line in result.stdout.splitlines() if line.strip()]

    def _server_version(self, clone_container: str) -> str:
        rows = self._psql(clone_container, "SHOW server_version")
        return rows[0][0] if rows else "unknown"

    def _schema_revision(self, clone_container: str) -> str:
        rows = self._psql(
            clone_container,
            "SELECT version_num FROM alembic_version",
        )
        return rows[0][0] if rows else "unknown"

    def _server_settings(self, clone_container: str) -> dict[str, str]:
        settings: dict[str, str] = {}
        for name in RECORDED_SETTINGS:
            rows = self._psql(clone_container, f"SHOW {name}")
            settings[name] = rows[0][0] if rows else "unknown"
        return settings

    def _database_identity(self, clone_container: str) -> dict[str, Any]:
        rows = self._psql(
            clone_container,
            "SELECT (SELECT system_identifier FROM pg_control_system())::text, "
            "current_database(), "
            "(SELECT oid FROM pg_database WHERE datname = current_database())::text, "
            "pg_database_size(current_database())::text",
        )
        if not rows:
            return {
                "system_identifier": "unknown",
                "database_name": DEFAULT_CLONE_DATABASE,
                "database_oid": "0",
                "size_bytes": 0,
            }
        row = rows[0]
        return {
            "system_identifier": row[0],
            "database_name": row[1],
            "database_oid": row[2],
            "size_bytes": int(row[3]),
        }

    def _index_inventory(self, clone_container: str) -> tuple[dict[str, Any], ...]:
        rows = self._psql(
            clone_container,
            "SELECT schemaname, tablename, indexname, "
            "COALESCE(pg_relation_size(format('%I.%I', schemaname, indexname)::regclass), 0)::text "
            "FROM pg_indexes WHERE schemaname = 'public' "
            "ORDER BY tablename, indexname",
        )
        return tuple(
            {
                "schema": row[0],
                "table": row[1],
                "index": row[2],
                "size_bytes": int(row[3]),
            }
            for row in rows
        )


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the clone_db command line parser."""
    parser = argparse.ArgumentParser(
        description="Manage writable benchmark database clones",
    )
    parser.add_argument(
        "--source-volume",
        default=DEFAULT_SOURCE_VOLUME,
        help="Immutable Docker volume holding the September production dump",
    )
    parser.add_argument("--run-id", required=True, help="Benchmark run identifier")
    parser.add_argument(
        "--postgres-config",
        default="production",
        choices=("production", "conservative"),
        help="PostgreSQL profile recorded with the clone",
    )
    parser.add_argument(
        "--action",
        default="create",
        choices=("create", "record", "query", "reset", "destroy", "seal-template"),
        help="Clone lifecycle action",
    )
    parser.add_argument(
        "--template-id",
        default=None,
        help="Use a sealed, stopped template for create or reset only",
    )
    parser.add_argument(
        "--source-container",
        default=DEFAULT_SOURCE_CONTAINER,
        help="Container currently serving the immutable source dump",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the clone_db command."""
    arguments = build_argument_parser().parse_args(argv)
    layout = BenchmarkLayout.from_path()
    manager = PostgresCloneManager(
        layout=layout,
        source_volume=arguments.source_volume,
        source_container=arguments.source_container,
    )
    try:
        if arguments.template_id is not None and arguments.action not in (
            "create",
            "reset",
        ):
            raise CloneCommandError("--template-id is valid only for create or reset")
        if arguments.action == "create":
            record = manager.create(
                run_id=arguments.run_id,
                profile=arguments.postgres_config,
                template_id=arguments.template_id,
            )
            result: Mapping[str, Any] = record.to_dict()
        elif arguments.action == "seal-template":
            result = manager.seal_template(run_id=arguments.run_id)
        elif arguments.action == "record":
            result = manager.record_existing(
                run_id=arguments.run_id,
                profile=arguments.postgres_config,
            ).to_dict()
        elif arguments.action == "query":
            result = manager.query(run_id=arguments.run_id)
        elif arguments.action == "reset":
            result = manager.reset(
                run_id=arguments.run_id,
                template_id=arguments.template_id,
            ).to_dict()
        else:
            result = manager.destroy(run_id=arguments.run_id)
    except (BenchmarkGuardError, CloneCommandError) as error:
        cli_support.print_result(
            {
                "command": "clone_db",
                "action": arguments.action,
                "status": "failed",
                "error": str(error),
            }
        )
        return cli_support.EXIT_GUARD_REFUSED
    cli_support.print_result(
        {
            "command": "clone_db",
            "action": arguments.action,
            "status": "ok",
            "clone_directory": str(layout.clone_directory(arguments.run_id)),
            "record": result,
        }
    )
    return cli_support.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
