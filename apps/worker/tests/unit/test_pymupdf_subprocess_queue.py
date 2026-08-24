"""Child workers must flush the multiprocessing queue before exiting."""

from __future__ import annotations

import app.services.document_parser.formats.pdf.pymupdf_subprocess as subprocess_mod
from shared.core.exceptions.domain_exceptions import PDFParsingException


class _FakeQueue:
    def __init__(self) -> None:
        self.items: list[object] = []
        self.closed = False
        self.joined = False

    def put(self, item: object) -> None:
        self.items.append(item)

    def close(self) -> None:
        self.closed = True

    def join_thread(self) -> None:
        self.joined = True


def test_worker_decorator_flushes_queue_after_success() -> None:
    @subprocess_mod.worker
    def _ok(queue: _FakeQueue, value: str) -> None:
        queue.put({"ok": True, "value": value})

    queue = _FakeQueue()
    _ok(queue, "payload")

    assert queue.items == [{"ok": True, "value": "payload"}]
    assert queue.closed is True
    assert queue.joined is True


def test_worker_decorator_flushes_queue_after_failure() -> None:
    @subprocess_mod.worker
    def _boom(queue: _FakeQueue) -> None:
        raise RuntimeError("child failed")

    queue = _FakeQueue()
    _boom(queue)

    assert queue.items[0]["ok"] is False
    assert queue.items[0]["error_type"] == "RuntimeError"
    assert queue.closed is True
    assert queue.joined is True


def test_empty_queue_exit_is_retryable() -> None:
    exc = PDFParsingException(
        user_message="Failed to process your document. Please try again.",
        reason="SUBPROCESS_CRASH",
        internal_message=(
            "pymupdf child exited with code=0 and no result: "
            "fn=_probe_assets_worker, pid=2049"
        ),
    )
    assert subprocess_mod._is_empty_queue_exit(exc) is True


def test_nonzero_crash_is_not_retryable() -> None:
    exc = PDFParsingException(
        user_message="Failed to process your document. Please try again.",
        reason="SUBPROCESS_CRASH",
        internal_message="pymupdf child exited with code=-9 and no result: fn=x, pid=1",
    )
    assert subprocess_mod._is_empty_queue_exit(exc) is False


def test_empty_queue_exit_retries_once(monkeypatch: object) -> None:
    calls = {"n": 0}
    crash = PDFParsingException(
        user_message="Failed to process your document. Please try again.",
        reason="SUBPROCESS_CRASH",
        internal_message=(
            "pymupdf child exited with code=0 and no result: "
            "fn=_probe_assets_worker, pid=2049"
        ),
    )

    def _once(_worker_fn: object, _args: tuple, _timeout: int) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise crash
        return {"ok": True, "assets": []}

    monkeypatch.setattr(subprocess_mod, "_run_worker_in_spawned_process_once", _once)
    result = subprocess_mod._run_worker_in_spawned_process(lambda: None, (), 10)
    assert result == {"ok": True, "assets": []}
    assert calls["n"] == 2
