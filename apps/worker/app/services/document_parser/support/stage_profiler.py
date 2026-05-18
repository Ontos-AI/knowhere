"""Structured stage timing helper for the document parsing pipeline."""

from contextlib import contextmanager
from time import perf_counter
from typing import Any, Iterator

from loguru import logger


def _compact_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values so timing logs stay compact and readable."""
    compacted_fields: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        compacted_fields[key] = value
    return compacted_fields


@contextmanager
def stage_timer(stage: str, **fields: Any) -> Iterator[None]:
    """Log elapsed time for a parsing stage without changing control flow."""
    start_time: float = perf_counter()
    compact_fields: dict[str, Any] = _compact_fields(fields)

    try:
        yield
    except Exception:
        elapsed_ms: int = int((perf_counter() - start_time) * 1000)
        logger.bind(
            event="document_parser.stage",
            stage=stage,
            elapsed_ms=elapsed_ms,
            status="error",
            **compact_fields,
        ).warning(f"Stage failed: {stage}")
        raise

    elapsed_ms = int((perf_counter() - start_time) * 1000)
    logger.bind(
        event="document_parser.stage",
        stage=stage,
        elapsed_ms=elapsed_ms,
        status="ok",
        **compact_fields,
    ).info(f"Stage completed: {stage}")
