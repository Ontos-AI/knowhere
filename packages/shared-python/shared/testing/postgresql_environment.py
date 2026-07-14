from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def configure_pytest_postgresql_for_current_platform() -> None:
    """Remove POSIX-only quoting from pytest-postgresql on Windows."""
    if os.name != "nt":
        return

    from pytest_postgresql.executor import PostgreSQLExecutor

    executor_type: Any = PostgreSQLExecutor
    executor_type.BASE_PROC_START_COMMAND = (
        '{executable} start -D "{datadir}" '
        '-o "-F -p {port} -c log_destination=stderr '
        '-c logging_collector=off {postgres_options}" '
        '-l "{logfile}" {startparams}'
    )

    def _stop_on_windows(
        executor: Any,
        _signal: int | None = None,
        _expected_signal: int | None = None,
    ) -> Any:
        subprocess.check_output(
            [
                executor.executable,
                "stop",
                "-D",
                executor.datadir,
                "-m",
                "f",
            ]
        )
        if executor.process is not None:
            executor.process.wait(timeout=10)
            executor._clear_process()
        return executor

    executor_type.stop = _stop_on_windows


def _get_common_postgresql_bin_directories() -> tuple[Path, ...]:
    common_directories: list[Path] = [
        Path("/opt/homebrew/opt/postgresql@16/bin"),
        Path("/opt/homebrew/opt/postgresql@15/bin"),
        Path("/usr/local/opt/postgresql@16/bin"),
        Path("/usr/local/opt/postgresql@15/bin"),
    ]
    linux_root: Path = Path("/usr/lib/postgresql")

    if linux_root.exists():
        versioned_directories: list[Path] = sorted(
            [child / "bin" for child in linux_root.iterdir() if child.is_dir()],
            reverse=True,
        )
        common_directories.extend(versioned_directories)

    return tuple(common_directories)


def find_executable(
    executable_name: str,
    search_paths: Sequence[Path] | None = None,
) -> Path | None:
    resolved_search_paths: Sequence[Path]

    if search_paths is not None:
        resolved_search_paths = search_paths
    else:
        path_directories: tuple[Path, ...] = tuple(
            Path(path_value)
            for path_value in os.environ.get("PATH", "").split(os.pathsep)
            if path_value
        )
        resolved_search_paths = (
            *path_directories,
            *_get_common_postgresql_bin_directories(),
        )

    for search_path in resolved_search_paths:
        candidate_path: Path = search_path / executable_name
        if candidate_path.is_file() and os.access(candidate_path, os.X_OK):
            return candidate_path

    return None
