from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from shared.services.retrieval.stats.service import record_retrieval_hits


_pending_retrieval_hit_stat_tasks: set[asyncio.Task[None]] = set()


def _finalize_retrieval_hit_stats_task(task: asyncio.Task[None]) -> None:
    _pending_retrieval_hit_stat_tasks.discard(task)

    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning(f'Failed to record retrieval hit stats (ignored): {exc}')


def schedule_retrieval_hit_stats_update(*, user_id: str, namespace: str, results: list[dict[str, Any]]) -> None:
    try:
        task = asyncio.create_task(
            _record_retrieval_hit_stats_best_effort(
                user_id=user_id,
                namespace=namespace,
                results=results,
            ),
            name=f'retrieval_hit_stats:{user_id}:{namespace}',
        )
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


async def _record_retrieval_hit_stats_best_effort(*, user_id: str, namespace: str, results: list[dict[str, Any]]) -> None:
    try:
        from shared.core.database import get_db_context

        async with get_db_context() as db:
            await record_retrieval_hits(db, user_id=user_id, namespace=namespace, results=results)
            await db.commit()
    except Exception as exc:
        logger.warning(f'Failed to record retrieval hit stats (ignored): {exc}')
