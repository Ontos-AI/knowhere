"""Run Markdown heading rules without blocking the Worker greenlets."""

from __future__ import annotations

import pickle
import sys
from typing import TYPE_CHECKING

from gevent import subprocess
from gevent.lock import BoundedSemaphore

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    TimeoutException,
    WorkerHandlingException,
)

if TYPE_CHECKING:
    import pandas as pd


_HEADING_PROCESS_SLOT: BoundedSemaphore = BoundedSemaphore(1)


def prepare_markdown_headings(
    lines: list[str], first_toc_start: int | None
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """Return rule-filtered candidates and the original rows before the TOC."""
    timeout_seconds: int = settings.TASK_TIME_LIMIT_SECONDS
    with _HEADING_PROCESS_SLOT:
        payload: bytes = pickle.dumps(
            (lines, first_toc_start), protocol=pickle.HIGHEST_PROTOCOL
        )
        with subprocess.Popen(
            [sys.executable, "-m", __name__],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        ) as process:
            try:
                output, _ = process.communicate(payload, timeout=timeout_seconds)
            except BaseException as error:
                if process.poll() is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                # Let gevent's pipe readers finish before Popen closes the streams.
                process.communicate()
                if isinstance(error, subprocess.TimeoutExpired):
                    raise TimeoutException(
                        internal_message=f"Markdown heading preparation exceeded {timeout_seconds}s",
                        retry_after=settings.DOCUMENT_INGESTION_TASK_RETRY_COUNTDOWN,
                    ) from error
                raise
            if process.returncode != 0:
                raise WorkerHandlingException(
                    internal_message=f"Markdown heading preparation exited with code {process.returncode}"
                )
        result: tuple[pd.DataFrame, pd.DataFrame | None] = pickle.loads(output)
    return result


def _run_child() -> None:
    from app.core.runtime_limits import apply_native_thread_caps

    apply_native_thread_caps()

    from app.services.document_parser.structure.heading_candidates import (
        estimate_heading_rule_levels,
        filter_markdown_headings,
    )

    lines, first_toc_start = pickle.load(sys.stdin.buffer)
    predictions: pd.DataFrame = filter_markdown_headings(lines)
    pre_toc_rows: pd.DataFrame | None = None
    if first_toc_start is not None and first_toc_start > 0:
        pre_toc_mask: pd.Series = predictions["id"] < first_toc_start
        if pre_toc_mask.any():
            pre_toc_rows = predictions[pre_toc_mask].copy()
            pre_toc_rows["level"] = -1
            predictions = predictions[~pre_toc_mask].reset_index(drop=True)

    result: tuple[pd.DataFrame, pd.DataFrame | None] = (
        estimate_heading_rule_levels(predictions),
        pre_toc_rows,
    )
    pickle.dump(result, sys.stdout.buffer, protocol=pickle.HIGHEST_PROTOCOL)


if __name__ == "__main__":
    _run_child()
