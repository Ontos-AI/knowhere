from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.stats.service import record_document_usage, record_retrieval_hits


_pending_retrieval_hit_stat_tasks: set[asyncio.Task[None]] = set()


def _finalize_retrieval_hit_stats_task(task: asyncio.Task[None]) -> None:
    _pending_retrieval_hit_stat_tasks.discard(task)

    try:
        task.result()
    except asyncio.CancelledError:
        logger.debug('Retrieval hit stats task was cancelled')
    except Exception as exc:
        logger.warning(f'Failed to record retrieval hit stats (ignored): {exc}')


def schedule_retrieval_hit_stats_update(*, user_id: str, namespace: str, results: list[dict[str, Any]]) -> None:
    _schedule_best_effort(
        lambda db: record_retrieval_hits(db, user_id=user_id, namespace=namespace, results=results),
        name=f'retrieval_hit_stats:{user_id}:{namespace}',
    )


def schedule_document_usage_update(*, user_id: str, namespace: str, items: list[dict[str, Any]]) -> None:
    """Record document uses off the request path; a failure only loses this record."""
    _schedule_best_effort(
        lambda db: record_document_usage(db, user_id=user_id, namespace=namespace, items=items),
        name=f'retrieval_document_usage:{user_id}:{namespace}',
    )


def _schedule_best_effort(record: Callable[[AsyncSession], Awaitable[Any]], *, name: str) -> None:
    try:
        task = asyncio.create_task(_record_best_effort(record), name=name)
        _pending_retrieval_hit_stat_tasks.add(task)
        task.add_done_callback(_finalize_retrieval_hit_stats_task)
    except Exception as exc:
        logger.warning(f'Failed to schedule retrieval hit stats update (ignored): {exc}')


async def drain_retrieval_hit_stats_updates(timeout_seconds: float = 2.0) -> None:
    if not _pending_retrieval_hit_stat_tasks:
        return

    pending_tasks = tuple(_pending_retrieval_hit_stat_tasks)

    try:
        await asyncio.wait_for(
            asyncio.gather(*pending_tasks, return_exceptions=True),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        for task in pending_tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*pending_tasks, return_exceptions=True)


async def _record_best_effort(record: Callable[[AsyncSession], Awaitable[Any]]) -> None:
    try:
        from shared.core.database import get_db_context

        async with get_db_context() as db:
            await record(db)
            await db.commit()
    except Exception as exc:
        logger.warning(f'Failed to record retrieval hit stats (ignored): {exc}')
