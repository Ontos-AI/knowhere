from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

import gevent
from gevent.pool import Pool as GeventPool
from loguru import logger

from app.services.document_parser.support.path_helpers import process_path_texts
from app.services.document_parser.support.parser_rows import (
    COL_ASSET_TITLE,
    COL_ENTITIES,
    COL_SUMMARY,
    serialize_entities,
)
from shared.core.config import settings
from shared.services.ai.summary.model import AssetSummary


@dataclass(frozen=True)
class DocxImageSummaryTask:
    image_hash: str
    image_path: str
    context: str
    usage_task: str
    title_path_prefix: str | None = None


@dataclass
class DocxImageOccurrence:
    image_hash: str
    image_index: str
    row: list[object]
    content_holder: list[Any] | None = None
    content_index: int | None = None


@dataclass
class DocxImageSummaryScheduler:
    should_summarize: bool
    _tasks_by_hash: dict[str, DocxImageSummaryTask] = field(default_factory=dict)
    _results_by_hash: dict[str, AssetSummary | None] = field(default_factory=dict)
    _relative_paths_by_hash: dict[str, str] = field(default_factory=dict)
    _occurrences_by_hash: dict[str, list[DocxImageOccurrence]] = field(
        default_factory=dict
    )

    def register_task(
        self,
        *,
        image_hash: str,
        image_path: str,
        context: str,
        usage_task: str,
        title_path_prefix: str | None = None,
    ) -> None:
        if not self.should_summarize or image_hash in self._tasks_by_hash:
            return

        self._tasks_by_hash[image_hash] = DocxImageSummaryTask(
            image_hash=image_hash,
            image_path=image_path,
            context=context,
            usage_task=usage_task,
            title_path_prefix=title_path_prefix,
        )

    def register_occurrence(
        self,
        occurrence: DocxImageOccurrence,
    ) -> None:
        self._occurrences_by_hash.setdefault(occurrence.image_hash, []).append(
            occurrence
        )
        if occurrence.image_hash in self._results_by_hash:
            self._apply_result(occurrence.image_hash, occurrence)

    def run_for_hashes(
        self,
        image_hashes: Iterable[str],
        *,
        stage: str,
        total_tasks: int,
    ) -> None:
        if not self.should_summarize:
            return

        unique_hashes = list(dict.fromkeys(image_hashes))
        pending_tasks = [
            self._tasks_by_hash[image_hash]
            for image_hash in unique_hashes
            if image_hash in self._tasks_by_hash
            and image_hash not in self._results_by_hash
        ]
        if not pending_tasks:
            return

        batch_total_tasks = sum(
            len(self._occurrences_by_hash.get(task.image_hash, []))
            for task in pending_tasks
        )
        self._run_batch(
            pending_tasks,
            stage=stage,
            total_tasks=batch_total_tasks or total_tasks,
        )
        for task in pending_tasks:
            for occurrence in self._occurrences_by_hash.get(task.image_hash, []):
                self._apply_result(task.image_hash, occurrence)

    def run_all(self) -> None:
        total_occurrences = sum(
            len(occurrences) for occurrences in self._occurrences_by_hash.values()
        )
        self.run_for_hashes(
            self._tasks_by_hash.keys(),
            stage="docx.image_summaries",
            total_tasks=total_occurrences,
        )

    def get_description(self, image_hash: str, image_index: str) -> str:
        result = self._results_by_hash.get(image_hash)
        if result is not None and result.summary:
            return result.summary
        return image_index

    def _run_batch(
        self,
        tasks: list[DocxImageSummaryTask],
        *,
        stage: str,
        total_tasks: int,
    ) -> int:
        max_concurrent = max(1, int(settings.DOCX_IMAGE_SUMMARY_MAX_CONCURRENT))
        pool_size = min(max_concurrent, len(tasks))
        start_time = perf_counter()
        pool = GeventPool(size=pool_size)
        greenlets = [pool.spawn(_run_summary_task, task) for task in tasks]
        gevent.joinall(greenlets)

        failed_tasks = 0
        for task, greenlet in zip(tasks, greenlets, strict=True):
            result = greenlet.value
            if result is None:
                failed_tasks += 1
            self._results_by_hash[task.image_hash] = result

        duration_ms = int((perf_counter() - start_time) * 1000)
        logger.bind(
            event="document_parser.docx_image_summary_batch",
            stage=stage,
            total_tasks=total_tasks,
            unique_tasks=len(tasks),
            max_concurrent=pool_size,
            duration_ms=duration_ms,
            failed_tasks=failed_tasks,
        ).info("Completed DOCX image summary batch")
        return failed_tasks

    def _apply_result(
        self,
        image_hash: str,
        occurrence: DocxImageOccurrence,
    ) -> None:
        result = self._results_by_hash.get(image_hash)
        if result is None:
            return

        summary = result.summary or ""
        asset_title = result.title or ""
        relative_path = self._resolve_relative_path(
            image_hash=image_hash,
            result=result,
            current_relative_path=str(occurrence.row[1]),
        )
        summary_field = (
            f"{occurrence.image_index}\n{summary}" if summary else occurrence.image_index
        )
        occurrence.row[0] = _build_image_ref(relative_path, summary)
        occurrence.row[1] = relative_path
        occurrence.row[3] = len(str(occurrence.row[0]))
        occurrence.row[COL_SUMMARY] = summary_field
        occurrence.row[COL_ENTITIES] = serialize_entities(result.entities)
        occurrence.row[COL_ASSET_TITLE] = asset_title

        if (
            occurrence.content_holder is not None
            and occurrence.content_index is not None
        ):
            occurrence.content_holder[occurrence.content_index] = occurrence.row[0]

    def _resolve_relative_path(
        self,
        *,
        image_hash: str,
        result: AssetSummary,
        current_relative_path: str,
    ) -> str:
        renamed_relative_path = self._relative_paths_by_hash.get(image_hash)
        if renamed_relative_path:
            return renamed_relative_path

        task = self._tasks_by_hash.get(image_hash)
        title = result.title or ""
        if task is None or not task.title_path_prefix or not title:
            return current_relative_path

        new_name = process_path_texts(f"{task.title_path_prefix} {title}", last=30)
        if not new_name:
            return current_relative_path

        image_dir = os.path.dirname(task.image_path)
        image_extension = os.path.splitext(task.image_path)[1]
        new_absolute_path = os.path.join(image_dir, f"{new_name}{image_extension}")
        if task.image_path != new_absolute_path:
            if not os.path.exists(task.image_path):
                return current_relative_path
            os.rename(task.image_path, new_absolute_path)

        relative_dir = os.path.dirname(current_relative_path)
        new_relative_path = f"{new_name}{image_extension}"
        if relative_dir:
            new_relative_path = f"{relative_dir}/{new_relative_path}"
        self._relative_paths_by_hash[image_hash] = new_relative_path
        return new_relative_path


def build_fallback_image_ref(relative_path: str) -> str:
    return _build_image_ref(relative_path, "")


def _build_image_ref(relative_path: str, summary: str) -> str:
    from shared.utils.chunk_refs import build_chunk_ref

    img_ref = build_chunk_ref(relative_path)
    if summary:
        return f"\n{summary}\n{img_ref}\n"
    return f"\n{img_ref}\n"


def _run_summary_task(task: DocxImageSummaryTask) -> AssetSummary | None:
    try:
        from shared.services.ai.summary.engine import summarize

        result = summarize(
            mode="asset",
            image_paths=[task.image_path],
            text=task.context,
            usage_task=task.usage_task,
        )
        if isinstance(result, AssetSummary):
            return result
        logger.warning(
            f"Invalid DOCX image summary result type: {type(result).__name__}"
        )
    except Exception as exc:
        logger.warning(
            f"DOCX image summary failed for hash={task.image_hash[:12]}...: {exc}"
        )
    return None
