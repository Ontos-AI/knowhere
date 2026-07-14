"""Stable anonymous installation identity for self-hosted telemetry."""

from __future__ import annotations

from contextlib import contextmanager
import importlib
import os
from pathlib import Path
from typing import Any, Iterator, TextIO
from uuid import UUID, uuid4


def get_or_create_installation_id(
    *,
    explicit_installation_id: str,
    installation_id_path: Path,
) -> str:
    """Resolve the stable anonymous self-hosted installation id.

    Operator-provided ids win. Otherwise, the id is generated once and stored in
    the persistent self-hosted secrets volume so restarts keep the same id.
    """
    normalized_explicit_id = explicit_installation_id.strip()
    if normalized_explicit_id:
        _parse_uuid(normalized_explicit_id)
        return normalized_explicit_id

    installation_id_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = installation_id_path.with_suffix(f"{installation_id_path.suffix}.lock")

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        with _exclusive_file_lock(lock_file):
            existing_installation_id = _read_valid_installation_id(
                installation_id_path
            )
            if existing_installation_id:
                return existing_installation_id

            generated_installation_id = str(uuid4())
            _write_installation_id_atomically(
                installation_id_path,
                generated_installation_id,
            )
            return generated_installation_id


@contextmanager
def _exclusive_file_lock(lock_file: TextIO) -> Iterator[None]:
    """Hold an exclusive lock using the current platform's native API."""
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write("\0")
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return

    fcntl: Any = importlib.import_module("fcntl")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_valid_installation_id(installation_id_path: Path) -> str:
    if not installation_id_path.exists():
        return ""

    candidate = installation_id_path.read_text(encoding="utf-8").strip()
    if not candidate:
        return ""

    try:
        _parse_uuid(candidate)
    except ValueError:
        return ""
    return candidate


def _parse_uuid(candidate: str) -> UUID:
    try:
        return UUID(candidate)
    except ValueError as exc:
        raise ValueError("TELEMETRY_INSTALLATION_ID must be a UUID") from exc


def _write_installation_id_atomically(
    installation_id_path: Path,
    installation_id: str,
) -> None:
    temporary_path = installation_id_path.with_name(
        f".{installation_id_path.name}.{os.getpid()}.tmp"
    )
    file_descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(f"{installation_id}\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, installation_id_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
