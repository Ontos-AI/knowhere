"""Unit tests for the stable self-hosted telemetry installation id."""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest

from shared.services.telemetry.identity import get_or_create_installation_id


def test_explicit_installation_id_wins(tmp_path: Path) -> None:
    installation_id_path = tmp_path / "telemetry-installation-id"
    explicit_installation_id = "550e8400-e29b-41d4-a716-446655440000"

    installation_id = get_or_create_installation_id(
        explicit_installation_id=explicit_installation_id,
        installation_id_path=installation_id_path,
    )

    assert installation_id == explicit_installation_id
    assert not installation_id_path.exists()


def test_explicit_installation_id_must_be_uuid(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be a UUID"):
        get_or_create_installation_id(
            explicit_installation_id="not-a-uuid",
            installation_id_path=tmp_path / "telemetry-installation-id",
        )


def test_missing_file_generates_uuid(tmp_path: Path) -> None:
    installation_id_path = tmp_path / "telemetry-installation-id"

    installation_id = get_or_create_installation_id(
        explicit_installation_id="",
        installation_id_path=installation_id_path,
    )

    UUID(installation_id)
    assert installation_id_path.read_text(encoding="utf-8").strip() == installation_id


def test_generated_id_is_stable_across_calls(tmp_path: Path) -> None:
    installation_id_path = tmp_path / "telemetry-installation-id"

    first = get_or_create_installation_id(
        explicit_installation_id="",
        installation_id_path=installation_id_path,
    )
    second = get_or_create_installation_id(
        explicit_installation_id="",
        installation_id_path=installation_id_path,
    )

    assert first == second


def test_invalid_existing_file_is_replaced(tmp_path: Path) -> None:
    installation_id_path = tmp_path / "telemetry-installation-id"
    installation_id_path.write_text("not-a-uuid\n", encoding="utf-8")

    installation_id = get_or_create_installation_id(
        explicit_installation_id="",
        installation_id_path=installation_id_path,
    )

    UUID(installation_id)
    assert installation_id_path.read_text(encoding="utf-8").strip() == installation_id


def test_concurrent_calls_return_the_same_id(tmp_path: Path) -> None:
    installation_id_path = tmp_path / "telemetry-installation-id"
    barrier = threading.Barrier(8)

    def _resolve() -> str:
        barrier.wait(timeout=5)
        return get_or_create_installation_id(
            explicit_installation_id="",
            installation_id_path=installation_id_path,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: _resolve(), range(8)))

    assert len(set(results)) == 1
    assert installation_id_path.read_text(encoding="utf-8").strip() == results[0]


@pytest.mark.skipif(sys.platform != "win32", reason="fcntl is POSIX-only")
def test_identity_import_does_not_require_fcntl() -> None:
    assert "fcntl" not in sys.modules
    from shared.services.telemetry import identity as identity_module

    assert identity_module.get_or_create_installation_id is get_or_create_installation_id
