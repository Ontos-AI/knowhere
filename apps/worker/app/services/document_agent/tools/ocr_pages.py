"""OCR specified PDF pages with RapidOCR and persist page text on the blackboard."""

from __future__ import annotations

import atexit
import multiprocessing
import time
from collections.abc import Mapping
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from threading import RLock
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict, cast

from loguru import logger

from app.services.document_agent.pdf_text import (
    LineDraft,
    PageTextBands,
    build_page_text_bands,
)
from shared.core.exceptions.domain_exceptions import PDFParsingException, TimeoutException

if TYPE_CHECKING:
    from gevent.lock import Semaphore
    from gevent.threadpool import ThreadPool


OCR_TIMEOUT_SECONDS = 1800
OCR_INTRA_OP_THREADS = 3
OCR_INTER_OP_THREADS = 1
OCR_QUEUE_POLL_INTERVAL_SECONDS = 0.1
OCR_CHILD_JOIN_TIMEOUT_SECONDS = 5
OCR_RUNNER_CONTEXT = multiprocessing.get_context("spawn")


class _OcrRequest(TypedDict):
    page: int
    image_path: str


class _OcrResponse(TypedDict):
    ok: bool
    page: NotRequired[int]
    lines: NotRequired[list[dict[str, Any]]]
    error_type: NotRequired[str]
    error_msg: NotRequired[str]


def _run_ocr_process(
    request_connection: Connection,
    response_connection: Connection,
) -> None:
    """Serve sequential OCR requests while keeping one model instance alive."""
    try:
        from rapidocr_onnxruntime import RapidOCR

        engine = RapidOCR(
            intra_op_num_threads=OCR_INTRA_OP_THREADS,
            inter_op_num_threads=OCR_INTER_OP_THREADS,
        )
    except Exception as exc:
        response_connection.send(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error_msg": str(exc),
            }
        )
        _close_child_connections(request_connection, response_connection)
        return

    try:
        while True:
            request = request_connection.recv()
            if request is None:
                return
            page = int(request["page"])
            image_path = str(request["image_path"])
            try:
                result, _elapse = engine(image_path)
                lines: list[dict[str, Any]] = []
                for item in result or []:
                    lines.append(
                        {
                            "box": _line_box(item),
                            "text": _line_text(item),
                            "score": _line_score(item),
                        }
                    )
                response_connection.send({"ok": True, "page": page, "lines": lines})
            except Exception as exc:
                response_connection.send(
                    {
                        "ok": False,
                        "page": page,
                        "error_type": type(exc).__name__,
                        "error_msg": str(exc),
                    }
                )
    finally:
        _close_child_connections(request_connection, response_connection)


def _close_child_connections(
    request_connection: Connection,
    response_connection: Connection,
) -> None:
    request_connection.close()
    response_connection.close()


def _close_parent_connection(connection: Connection) -> None:
    try:
        connection.close()
    except Exception as exc:
        logger.debug("Ignoring error while closing parent connection: {}", exc)


class _OcrRunner:
    """Own one persistent OCR child and serialize page requests through it."""

    def __init__(self) -> None:
        self._process: BaseProcess | None = None
        self._request_connection: Connection | None = None
        self._response_connection: Connection | None = None
        self._executor: ThreadPool | None = None
        self._slot: Semaphore | None = None
        self._lock = RLock()

    def run_page(self, page: int, image_path: str, *, timeout: int) -> _OcrResponse:
        deadline = time.monotonic() + timeout
        slot = self._get_slot()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not slot.acquire(timeout=remaining):
            raise TimeoutException(
                internal_message=(
                    f"OCR request waited more than {timeout}s for the shared runner: "
                    f"page={page}"
                ),
                retry_after=30,
            )

        task = None
        try:
            executor = self._get_executor()
            task = executor.spawn(
                self._run_page_blocking,
                page,
                image_path,
                deadline,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutException(
                    internal_message=(
                        f"OCR request timed out before execution: page={page}"
                    ),
                    retry_after=30,
                )
            raw_result = task.get(timeout=remaining)
            if not isinstance(raw_result, dict):
                raise PDFParsingException(
                    user_message="Failed to process your document. Please try again.",
                    reason="SUBPROCESS_FAILED",
                    internal_message=f"OCR runner returned an invalid result: page={page}",
                )
            return cast(_OcrResponse, raw_result)
        except BaseException as exc:
            self._cancel_active_process()
            if task is not None:
                try:
                    task.get(timeout=OCR_CHILD_JOIN_TIMEOUT_SECONDS)
                except BaseException as join_exc:
                    # Best-effort cleanup: preserve the original exception `exc`.
                    logger.debug(
                        "Ignoring OCR task join failure during cancellation "
                        "(page={}, join_timeout={}s): {}: {}",
                        page,
                        OCR_CHILD_JOIN_TIMEOUT_SECONDS,
                        type(join_exc).__name__,
                        join_exc,
                    )
            from gevent.timeout import Timeout

            if isinstance(exc, Timeout):
                raise TimeoutException(
                    internal_message=(
                        f"OCR request timed out after {timeout}s: page={page}"
                    ),
                    retry_after=30,
                ) from exc
            raise
        finally:
            slot.release()

    def close(self) -> None:
        process, request_connection, response_connection = self._detach_process()
        self._stop_process(process, request_connection, response_connection)

        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.kill()
            executor.join()

    def _get_executor(self) -> ThreadPool:
        if self._executor is None:
            from gevent.threadpool import ThreadPool

            self._executor = ThreadPool(maxsize=1)
        return self._executor

    def _get_slot(self) -> Semaphore:
        if self._slot is None:
            from gevent.lock import Semaphore

            self._slot = Semaphore(1)
        return self._slot

    def _run_page_blocking(
        self,
        page: int,
        image_path: str,
        deadline: float,
    ) -> _OcrResponse:
        self._ensure_process()
        request_connection = self._request_connection
        response_connection = self._response_connection
        process = self._process
        if request_connection is None or response_connection is None or process is None:
            raise RuntimeError("OCR runner failed to initialize")

        request: _OcrRequest = {"page": page, "image_path": image_path}
        request_connection.send(request)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop_active_process(
                        process, request_connection, response_connection
                    )
                    raise TimeoutException(
                        internal_message=(
                            f"OCR child timed out after {OCR_TIMEOUT_SECONDS}s: "
                            f"page={page}, pid={process.pid}"
                        ),
                        retry_after=30,
                    )
                try:
                    if response_connection.poll(
                        timeout=min(remaining, OCR_QUEUE_POLL_INTERVAL_SECONDS)
                    ):
                        result = response_connection.recv()
                        break
                    if process.is_alive():
                        continue
                    self._stop_active_process(
                        process, request_connection, response_connection
                    )
                    raise PDFParsingException(
                        user_message="Failed to process your document. Please try again.",
                        reason="SUBPROCESS_CRASH",
                        internal_message=(
                            f"OCR child exited without a result: page={page}, "
                            f"pid={process.pid}"
                        ),
                    )
                except Exception as exc:
                    self._stop_active_process(
                        process, request_connection, response_connection
                    )
                    raise PDFParsingException(
                        user_message="Failed to process your document. Please try again.",
                        reason="SUBPROCESS_FAILED",
                        internal_message=(
                            f"OCR child response queue failed on page {page}: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    ) from exc
        except BaseException:
            raise

        if not isinstance(result, dict):
            self._stop_active_process(
                process, request_connection, response_connection
            )
            raise PDFParsingException(
                user_message="Failed to process your document. Please try again.",
                reason="SUBPROCESS_FAILED",
                internal_message=f"OCR child returned an invalid response: page={page}",
            )
        if not result.get("ok"):
            error_type = result.get("error_type", "UnknownError")
            error_msg = result.get("error_msg", "unknown OCR child failure")
            raise PDFParsingException(
                user_message="Failed to process your document. Please try again.",
                reason="SUBPROCESS_FAILED",
                internal_message=(
                    f"OCR child failed on page {page}: {error_type}: {error_msg}"
                ),
            )
        return cast(_OcrResponse, result)

    def _ensure_process(self) -> None:
        with self._lock:
            if self._process is not None and self._process.is_alive():
                return
            process = self._process
            request_connection = self._request_connection
            response_connection = self._response_connection
            self._process = None
            self._request_connection = None
            self._response_connection = None
            self._stop_process(process, request_connection, response_connection)

            child_request_connection, request_connection = OCR_RUNNER_CONTEXT.Pipe(
                duplex=False
            )
            response_connection, child_response_connection = OCR_RUNNER_CONTEXT.Pipe(
                duplex=False
            )
            process = OCR_RUNNER_CONTEXT.Process(
                target=_run_ocr_process,
                args=(child_request_connection, child_response_connection),
            )
            self._request_connection = request_connection
            self._response_connection = response_connection
            self._process = process
            try:
                process.start()
            except BaseException:
                self._process = None
                self._request_connection = None
                self._response_connection = None
                self._stop_process(
                    process,
                    request_connection,
                    response_connection,
                    child_request_connection,
                    child_response_connection,
                )
                raise
            child_request_connection.close()
            child_response_connection.close()
            logger.info("[ocr-runner] started persistent OCR child pid={}", process.pid)

    def _detach_process(
        self,
    ) -> tuple[
        BaseProcess | None,
        Connection | None,
        Connection | None,
    ]:
        with self._lock:
            process = self._process
            request_connection = self._request_connection
            response_connection = self._response_connection
            self._process = None
            self._request_connection = None
            self._response_connection = None
        return process, request_connection, response_connection

    def _cancel_active_process(self) -> None:
        process, request_connection, response_connection = self._detach_process()
        self._stop_process(process, request_connection, response_connection)

    def _stop_active_process(
        self,
        process: BaseProcess,
        request_connection: Connection,
        response_connection: Connection,
    ) -> None:
        with self._lock:
            if self._process is not process:
                return
            self._process = None
            self._request_connection = None
            self._response_connection = None
        self._stop_process(process, request_connection, response_connection)

    @staticmethod
    def _stop_process(
        process: BaseProcess | None,
        request_connection: Connection | None,
        response_connection: Connection | None,
        *child_connections: Connection,
    ) -> None:
        if process is not None and process.pid is not None:
            if process.is_alive():
                process.kill()
            process.join(timeout=OCR_CHILD_JOIN_TIMEOUT_SECONDS)
        for connection in (request_connection, response_connection, *child_connections):
            if connection is not None:
                _close_parent_connection(connection)


_OCR_RUNNER = _OcrRunner()
atexit.register(_OCR_RUNNER.close)


def shutdown_ocr_runner() -> None:
    """Stop the worker-local OCR runner during Celery shutdown."""
    _OCR_RUNNER.close()


def _line_text(item: Any) -> str:
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return str(item[1] or "")
    return ""


def _line_box(item: Any) -> Any:
    if isinstance(item, (list, tuple)) and len(item) >= 1:
        return item[0]
    return None


def _line_score(item: Any) -> float:
    if isinstance(item, (list, tuple)) and len(item) >= 3:
        try:
            return float(item[2])
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def ocr_rendered_pages(
    image_path_by_page: Mapping[int, str],
    *,
    timeout: int = OCR_TIMEOUT_SECONDS,
) -> dict[int, str]:
    """Run RapidOCR on already-rendered page images and return ``{page: text}``.

    Per-page failures log a warning and skip the failing page so callers keep
    M0 text for that page without aborting sibling pages in the scope.
    """
    extracted: dict[int, str] = {}
    for raw_page in sorted(image_path_by_page):
        page = int(raw_page)
        image_path = str(image_path_by_page.get(raw_page) or "").strip()
        if page <= 0 or not image_path:
            continue
        try:
            result = _OCR_RUNNER.run_page(
                page,
                image_path,
                timeout=timeout,
            )
            lines = list(result.get("lines") or [])
            bands: PageTextBands = build_page_text_bands(
                LineDraft(text=str(line.get("text") or ""))
                for line in lines
                if isinstance(line, dict)
            )
            text = bands.content.strip()
            if text:
                extracted[page] = text
        except Exception as exc:
            logger.warning(
                "[ocr-runner] OCR failed on page {}; keeping M0 text: {}",
                page,
                exc,
            )
    return extracted

