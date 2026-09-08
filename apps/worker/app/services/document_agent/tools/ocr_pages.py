"""OCR specified PDF pages with RapidOCR and persist page text on the blackboard."""

from __future__ import annotations

import atexit
import multiprocessing
import queue as queue_module
import time
from dataclasses import dataclass
from multiprocessing.process import BaseProcess
from multiprocessing.queues import Queue as MultiprocessingQueue
from threading import RLock
from typing import Any

from loguru import logger

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.pdf_text import PageTextBands
from app.services.document_agent.registry import has_page_features, register_tool
from app.services.document_agent.visual import render_pages
from shared.core.exceptions.domain_exceptions import PDFParsingException, TimeoutException


OCR_TIMEOUT_SECONDS = 300
OCR_RUNNER_CONTEXT = multiprocessing.get_context("spawn")


@dataclass(frozen=True)
class _OcrRequestResult:
    status: str
    result: dict[str, Any] | None = None


def _run_ocr_process(
    request_queue: MultiprocessingQueue,
    response_queue: MultiprocessingQueue,
) -> None:
    """Serve sequential OCR requests while keeping one model instance alive."""
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
    while True:
        request = request_queue.get()
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
            response_queue.put({"ok": True, "page": page, "lines": lines})
        except Exception as exc:
            response_queue.put(
                {
                    "ok": False,
                    "page": page,
                    "error_type": type(exc).__name__,
                    "error_msg": str(exc),
                }
            )


class _OcrRunner:
    """Own one persistent OCR child and serialize page requests through it."""

    def __init__(self) -> None:
        self._process: BaseProcess | None = None
        self._request_queue: MultiprocessingQueue | None = None
        self._response_queue: MultiprocessingQueue | None = None
        self._executor: Any | None = None
        self._lock = RLock()

    def run_page(self, page: int, image_path: str, *, timeout: int) -> dict[str, Any]:
        executor = self._get_executor()
        return executor.apply(self._run_page_blocking, (page, image_path, timeout))

    def close(self) -> None:
        with self._lock:
            process = self._process
            request_queue = self._request_queue
            response_queue = self._response_queue
            self._process = None
            self._request_queue = None
            self._response_queue = None
        if process is not None and process.is_alive() and request_queue is not None:
            request_queue.put(None)
            process.join(timeout=5)
        if process is not None and process.is_alive():
            process.kill()
            process.join(timeout=5)
        for result_queue in (request_queue, response_queue):
            if result_queue is not None:
                result_queue.close()

    def _get_executor(self) -> Any:
        if self._executor is None:
            from gevent.threadpool import ThreadPool

            self._executor = ThreadPool(maxsize=1)
        return self._executor

    def _run_page_blocking(self, page: int, image_path: str, timeout: int) -> dict[str, Any]:
        with self._lock:
            self._ensure_process()
            request_queue = self._request_queue
            response_queue = self._response_queue
            process = self._process
            if request_queue is None or response_queue is None or process is None:
                raise RuntimeError("OCR runner failed to initialize")
            request_queue.put({"page": page, "image_path": image_path})
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate_process()
                    raise TimeoutException(
                        internal_message=(
                            f"OCR child timed out after {timeout}s: page={page}, "
                            f"pid={process.pid}"
                        ),
                        retry_after=30,
                    )
                try:
                    result = response_queue.get(
                        timeout=min(remaining, 0.1)
                    )
                    break
                except queue_module.Empty:
                    if process.is_alive():
                        continue
                    self._terminate_process()
                    raise PDFParsingException(
                        user_message="Failed to process your document. Please try again.",
                        reason="SUBPROCESS_CRASH",
                        internal_message=(
                            f"OCR child exited without a result: page={page}, "
                            f"pid={process.pid}"
                        ),
                    )
            if not process.is_alive() and not result:
                self._terminate_process()
                raise PDFParsingException(
                    user_message="Failed to process your document. Please try again.",
                    reason="SUBPROCESS_CRASH",
                    internal_message=f"OCR child exited without a result: page={page}",
                )
            if not result.get("ok"):
                raise PDFParsingException(
                    user_message="Failed to process your document. Please try again.",
                    reason="SUBPROCESS_FAILED",
                    internal_message=(
                        f"OCR child failed on page {page}: "
                        f"{result.get('error_type')}: {result.get('error_msg')}"
                    ),
                )
            return result

    def _ensure_process(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        self.close()
        self._request_queue = OCR_RUNNER_CONTEXT.Queue()
        self._response_queue = OCR_RUNNER_CONTEXT.Queue()
        self._process = OCR_RUNNER_CONTEXT.Process(
            target=_run_ocr_process,
            args=(self._request_queue, self._response_queue),
        )
        self._process.start()
        logger.info("[ocr-runner] started persistent OCR child pid={}", self._process.pid)

    def _terminate_process(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.is_alive():
            process.kill()
            process.join(timeout=5)


_OCR_RUNNER = _OcrRunner()
atexit.register(_OCR_RUNNER.close)


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


@register_tool(
    name="ocr.pages",
    description="Run RapidOCR on specified pages and return positioned text lines.",
    parameters={
        "type": "object",
        "properties": {
            "pages": {
                "type": "array",
                "items": {"type": "integer"},
            },
        },
        "required": ["pages"],
    },
    preconditions=(has_page_features,),
)
def ocr_pages(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    raw_pages = args.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        return ToolResult(
            status="error",
            error="ocr.pages requires pages",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    pages = [int(page) for page in raw_pages]
    pngs = render_pages(
        ctx,
        pages,
        folder_name="ocr_pages",
        prefix="ocr",
        timeout=300,
    )
    png_by_page = {
        int(item["page"]): str(item["png_path"])
        for item in pngs
        if item.get("page") is not None and item.get("png_path")
    }

    page_lines: dict[int, list[dict[str, Any]]] = {}
    for page in pages:
        image_path = png_by_page.get(page)
        if not image_path:
            page_lines[page] = []
            continue
        result = _OCR_RUNNER.run_page(
            page,
            image_path,
            timeout=OCR_TIMEOUT_SECONDS,
        )
        result_page = int(result.get("page") or page)
        page_lines[result_page] = list(result.get("lines") or [])
    page_texts: dict[int, str] = {}
    page_bands: dict[int, PageTextBands] = {}
    for page in pages:
        lines = page_lines.get(page, [])
        page_lines[page] = lines
        content = "\n".join(line["text"] for line in lines if line["text"])
        page_texts[page] = content
        # OCR has no reliable Y bands; content only, empty header/footer.
        page_bands[page] = PageTextBands(content=content)

    cache = dict(ctx.blackboard.page_full_text_cache)
    cache.update(page_bands)
    ctx.blackboard.page_full_text_cache = cache
    ctx.blackboard.page_text_search_view = None
    return ToolResult(
        status="ok",
        payload={"page_texts": page_texts, "page_lines": page_lines},
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary={"page_count": len(page_texts)},
    )
