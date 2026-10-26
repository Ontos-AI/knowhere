from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from shared.testing.postgresql_environment import find_executable


@dataclass(frozen=True)
class EnvironmentCheckResult:
    name: str
    is_available: bool
    detail: str
    remediation: str


@dataclass(frozen=True)
class InstallCommand:
    description: str
    command: tuple[str, ...]


_REQUIRED_PYTHON_MODULES: tuple[tuple[str, str], ...] = (
    ("fakeredis.aioredis", "fakeredis"),
    ("pytest_postgresql", "pytest-postgresql"),
    ("asyncpg", "asyncpg"),
    ("psycopg2", "psycopg2-binary"),
)
_REQUIRED_POSTGRESQL_EXECUTABLES: tuple[str, ...] = (
    "initdb",
    "pg_ctl",
    "postgres",
    "pg_config",
)
_REQUIRED_POSTGRESQL_EXTENSIONS: tuple[str, ...] = (
    "uuid-ossp",
    "pg_trgm",
    "vector",
)


def _get_postgresql_install_hint() -> str:
    return (
        "Run `uv run python apps/api/scripts/ensure_test_environment.py --install`, "
        "or install PostgreSQL server binaries, contrib extensions, and pgvector. "
        "Ubuntu: sudo apt-get update && sudo apt-get install -y "
        "postgresql postgresql-contrib, then install postgresql-<major>-pgvector "
        "from PGDG. macOS: brew install postgresql@16 pgvector "
        "and add its bin directory to PATH. If needed, set "
        "PYTEST_POSTGRESQL_EXECUTABLE to the pg_ctl path."
    )


def check_python_module(import_name: str, package_name: str) -> EnvironmentCheckResult:
    try:
        module_spec = importlib.util.find_spec(import_name)
    except ModuleNotFoundError:
        module_spec = None

    if module_spec is not None:
        return EnvironmentCheckResult(
            name=package_name,
            is_available=True,
            detail=f"Python module {import_name} is importable.",
            remediation="No action needed.",
        )

    return EnvironmentCheckResult(
        name=package_name,
        is_available=False,
        detail=f"Python module {import_name} is not importable.",
        remediation="Run `uv sync --all-packages --dev` from the repository root.",
    )


def _check_postgresql_executable(executable_name: str) -> EnvironmentCheckResult:
    executable_path: Path | None = find_executable(executable_name)

    if executable_path is not None:
        return EnvironmentCheckResult(
            name=executable_name,
            is_available=True,
            detail=f"Found {executable_path}.",
            remediation="No action needed.",
        )

    return EnvironmentCheckResult(
        name=executable_name,
        is_available=False,
        detail=f"Could not find executable {executable_name}.",
        remediation=_get_postgresql_install_hint(),
    )


def _get_postgresql_shared_directory() -> Path | None:
    pg_config_path: Path | None = find_executable("pg_config")

    if pg_config_path is None:
        return None

    result: subprocess.CompletedProcess[str] = subprocess.run(
        [str(pg_config_path), "--sharedir"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        return None

    shared_directory_value: str = result.stdout.strip()
    if not shared_directory_value:
        return None

    return Path(shared_directory_value)


def _check_postgresql_extension(extension_name: str) -> EnvironmentCheckResult:
    shared_directory: Path | None = _get_postgresql_shared_directory()

    if shared_directory is None:
        return EnvironmentCheckResult(
            name=extension_name,
            is_available=False,
            detail="Could not locate the PostgreSQL shared directory.",
            remediation=_get_postgresql_install_hint(),
        )

    control_file_path: Path = (
        shared_directory / "extension" / f"{extension_name}.control"
    )

    if control_file_path.exists():
        return EnvironmentCheckResult(
            name=extension_name,
            is_available=True,
            detail=f"Found {control_file_path}.",
            remediation="No action needed.",
        )

    return EnvironmentCheckResult(
        name=extension_name,
        is_available=False,
        detail=f"Missing PostgreSQL extension control file {control_file_path}.",
        remediation=_get_postgresql_install_hint(),
    )


def get_environment_check_results() -> tuple[EnvironmentCheckResult, ...]:
    python_results: tuple[EnvironmentCheckResult, ...] = tuple(
        check_python_module(import_name, package_name)
        for import_name, package_name in _REQUIRED_PYTHON_MODULES
    )
    executable_results: tuple[EnvironmentCheckResult, ...] = tuple(
        _check_postgresql_executable(executable_name)
        for executable_name in _REQUIRED_POSTGRESQL_EXECUTABLES
    )
    extension_results: tuple[EnvironmentCheckResult, ...] = tuple(
        _check_postgresql_extension(extension_name)
        for extension_name in _REQUIRED_POSTGRESQL_EXTENSIONS
    )

    return (*python_results, *executable_results, *extension_results)


def _format_check_result(result: EnvironmentCheckResult) -> str:
    status: str = "OK" if result.is_available else "MISSING"
    return f"[{status}] {result.name}: {result.detail}"


def _should_use_sudo() -> bool:
    get_effective_user_id = getattr(os, "geteuid", None)

    if callable(get_effective_user_id) and get_effective_user_id() == 0:
        return False

    return shutil.which("sudo") is not None


def _with_privilege(command: tuple[str, ...]) -> tuple[str, ...]:
    if _should_use_sudo():
        return ("sudo", *command)

    return command


def get_install_commands() -> tuple[InstallCommand, ...]:
    system_name: str = platform.system().lower()

    if system_name == "linux" and shutil.which("apt-get") is not None:
        return (
            InstallCommand(
                description="Refresh apt package metadata",
                command=_with_privilege(("apt-get", "update")),
            ),
            InstallCommand(
                description="Install PostgreSQL server binaries and contrib extensions",
                command=_with_privilege(
                    ("apt-get", "install", "-y", "postgresql", "postgresql-contrib")
                ),
            ),
            InstallCommand(
                description="Enable the PostgreSQL APT repository",
                command=_with_privilege(
                    (
                        "/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh",
                        "-y",
                    )
                ),
            ),
            InstallCommand(
                description="Upgrade PostgreSQL from PGDG and install matching pgvector",
                command=_with_privilege(
                    (
                        "bash",
                        "-lc",
                        (
                            "apt-get install -y postgresql postgresql-contrib && "
                            'apt-get install -y "postgresql-'
                            "$(pg_config --version | awk '{print $2}' | cut -d. -f1)"
                            '-pgvector"'
                        ),
                    )
                ),
            ),
        )

    if system_name == "darwin" and shutil.which("brew") is not None:
        return (
            InstallCommand(
                description="Install PostgreSQL server binaries and contrib extensions",
                command=("brew", "install", "postgresql@16", "pgvector"),
            ),
        )

    return ()


def _format_shell_command(command: Sequence[str]) -> str:
    return " ".join(command)


def _print_install_commands() -> int:
    install_commands: tuple[InstallCommand, ...] = get_install_commands()

    if not install_commands:
        print("No supported automatic install command was detected for this system.")
        print(_get_postgresql_install_hint())
        return 1

    for install_command in install_commands:
        print(f"{install_command.description}:")
        print(f"  {_format_shell_command(install_command.command)}")

    return 0


def _run_install_commands() -> int:
    install_commands: tuple[InstallCommand, ...] = get_install_commands()

    if not install_commands:
        print("No supported automatic install command was detected for this system.")
        print(_get_postgresql_install_hint())
        return 1

    for install_command in install_commands:
        print(f"\n{install_command.description}")
        print(f"$ {_format_shell_command(install_command.command)}")
        result: subprocess.CompletedProcess[bytes] = subprocess.run(
            install_command.command,
            check=False,
        )

        if result.returncode != 0:
            print(f"Install command failed with exit code {result.returncode}.")
            return result.returncode

    return 0


def _parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify or install API/worker test environment prerequisites.",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install PostgreSQL binaries/extensions before verifying the environment.",
    )
    parser.add_argument(
        "--print-install-command",
        action="store_true",
        help="Print detected install commands without running them.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_arguments(argv)

    if arguments.print_install_command:
        return _print_install_commands()

    if arguments.install:
        install_result: int = _run_install_commands()
        if install_result != 0:
            return install_result

    results: tuple[EnvironmentCheckResult, ...] = get_environment_check_results()
    missing_results: tuple[EnvironmentCheckResult, ...] = tuple(
        result for result in results if not result.is_available
    )

    for result in results:
        print(_format_check_result(result))

    if missing_results:
        print("\nRequired setup:")
        for result in missing_results:
            print(f"- {result.name}: {result.remediation}")
        print("\nAutomatic setup:")
        print("- Run `uv run python apps/api/scripts/ensure_test_environment.py --install`.")
        return 1

    print("\nContract test environment is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
