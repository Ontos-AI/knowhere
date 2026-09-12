"""Stable anonymous installation identity for self-hosted telemetry."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO
from uuid import UUID, uuid4

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_PROCESS_LOCK = threading.Lock()


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
def _exclusive_file_lock(lock_file: IO[str]) -> Iterator[None]:
    """Cross-process file lock plus an in-process mutex.

    POSIX uses ``fcntl.flock``. Windows ``msvcrt.locking`` is per-process, so a
    thread lock is required for concurrent callers in the same interpreter.
    """
    with _PROCESS_LOCK:
        _lock_exclusive(lock_file)
        try:
            yield
        finally:
            _unlock_exclusive(lock_file)


def _lock_exclusive(lock_file: IO[str]) -> None:
    if sys.platform == "win32":
        _ensure_lock_byte(lock_file)
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        return
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _unlock_exclusive(lock_file: IO[str]) -> None:
    if sys.platform == "win32":
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _ensure_lock_byte(lock_file: IO[str]) -> None:
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write("0")
        lock_file.flush()
        os.fsync(lock_file.fileno())


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
