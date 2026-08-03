from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal

import gevent
from app.services.document_parser.formats.markdown.deferred_task import (
    ImageDeferredSummaryTask,
    MarkdownDeferredSummaryTask,
    TableDeferredSummaryTask,
    TextDeferredSummaryTask,
)
from app.services.document_parser.support.stage_profiler import stage_timer
from app.services.document_parser.tables.table_text_parser import sanitize_table_name_from_header
from app.services.document_parser.support.parser_rows import (
    COL_ASSET_TITLE,
    COL_ENTITIES,
    COL_KEYWORDS,
    COL_SUMMARY,
    apply_body_summary,
    serialize_entities,
)
from gevent.pool import Pool as GeventPool
from loguru import logger

from shared.core.config import settings
from shared.services.ai.summary.engine import summarize
from shared.services.ai.summary.model import AssetSummary, BodySummary
from shared.utils.chunk_refs import build_chunk_ref
from app.services.common.file_utils import MAX_ASSET_FILE_NAME_CHARS, path_handle

# Each deferred task now carries the engine's typed contract straight through to
# the apply step (audit §4.5): assets → AssetSummary, text → BodySummary. The row
# is then written by name via apply_asset_summary / apply_body_summary, so we no
# longer poke magic positional offsets.
DeferredResult = tuple[int, Literal["image", "table", "text"], object]


@dataclass(frozen=True)
class MarkdownDeferredSummaryInput:
    rows: list[list[str | int]]
    tasks: list[MarkdownDeferredSummaryTask]
    output_dir: str
    summary_len: int = 1500


def apply_markdown_deferred_summaries(
    deferred_input: MarkdownDeferredSummaryInput,
) -> None:
    if not deferred_input.tasks:
        return

    image_task_count = sum(
        1 for task in deferred_input.tasks if isinstance(task, ImageDeferredSummaryTask)
    )
    table_task_count = sum(
        1 for task in deferred_input.tasks if isinstance(task, TableDeferredSummaryTask)
    )
    text_task_count = sum(
        1 for task in deferred_input.tasks if isinstance(task, TextDeferredSummaryTask)
    )
    logger.info(
        f"Running {len(deferred_input.tasks)} deferred summary LLM calls in parallel"
    )
    max_concurrent = getattr(settings, "SUMMARY_LLM_MAX_CONCURRENT", 8)

    with stage_timer(
        "md.deferred_summaries",
        total_tasks=len(deferred_input.tasks),
        image_tasks=image_task_count,
        table_tasks=table_task_count,
        text_tasks=text_task_count,
        max_concurrent=min(max_concurrent, len(deferred_input.tasks)),
    ):
        results = _run_deferred_summary_tasks(deferred_input, max_concurrent)
        _apply_deferred_summary_results(deferred_input, results)

    logger.info(f"Completed {len(deferred_input.tasks)} deferred summary LLM calls")


def replace_chunk_ref_in_rows(
    rows: list[list[str | int]], old_path: str, new_path: str
) -> None:
    """Rewrite asset paths after deferred rename.

    Text/image rows store bracketed refs (``[tables/...]`` / ``[images/...]``).
    Table rows store the bare relative path as ``content``. Both forms must move
    with the renamed file; historically only the bracketed form was updated,
    leaving table ``content`` stuck on the pre-rename path.
    """
    if not old_path or old_path == new_path:
        return

    old_ref = build_chunk_ref(old_path)
    new_ref = build_chunk_ref(new_path)

    for row in rows:
        if len(row) > 0 and isinstance(row[0], str):
            updated = row[0]
            if old_ref and new_ref and old_ref != new_ref:
                updated = updated.replace(old_ref, new_ref)
            # Table asset rows use the bare path as content (no brackets).
            if updated == old_path:
                updated = new_path
            elif old_path in updated:
                updated = updated.replace(old_path, new_path)
            row[0] = updated
        if len(row) > 1 and row[1] == old_path:
            row[1] = new_path
        if len(row) > 2 and isinstance(row[2], str):
            updated_type = row[2]
            if old_ref and new_ref and old_ref != new_ref:
                updated_type = updated_type.replace(old_ref, new_ref)
            if old_path in updated_type:
                updated_type = updated_type.replace(old_path, new_path)
            row[2] = updated_type
        if len(row) > 8 and isinstance(row[8], str):
            updated_connect = row[8]
            if old_ref and new_ref and old_ref != new_ref:
                updated_connect = updated_connect.replace(old_ref, new_ref)
            if old_path in updated_connect:
                updated_connect = updated_connect.replace(old_path, new_path)
            row[8] = updated_connect


def _run_deferred_summary_tasks(
    deferred_input: MarkdownDeferredSummaryInput,
    max_concurrent: int,
) -> list[DeferredResult | None]:
    pool = GeventPool(size=min(max_concurrent, len(deferred_input.tasks)))
    greenlets = [
        pool.spawn(_run_deferred_summary_task, task, deferred_input)
        for task in deferred_input.tasks
    ]
    gevent.joinall(greenlets)
    return [greenlet.value for greenlet in greenlets]


def _run_deferred_summary_task(
    task: MarkdownDeferredSummaryTask,
    deferred_input: MarkdownDeferredSummaryInput,
) -> DeferredResult | None:
    try:
        if isinstance(task, ImageDeferredSummaryTask):
            # Asset contract (§4.1): title + summary + entities from the image.
            # The engine's per-type image prompt covers charts/tables/diagrams and
            # pure-text images alike, closing the old MinerU "pure text image" gap.
            image_path = os.path.join(deferred_input.output_dir, task.relative_path)
            result = summarize(
                mode="asset",
                image_paths=[image_path],
                usage_task="parser.image.deferred",
            )
            return task.row_index, "image", result

        if isinstance(task, TableDeferredSummaryTask):
            # Tables are assets: summarize from HTML → title + summary + entities.
            result = summarize(
                mode="asset",
                text=task.table_html,
                usage_task="parser.table.deferred",
            )
            return task.row_index, "table", result

        if isinstance(task, TextDeferredSummaryTask):
            # Body content (Contract A): summary + entities, no title.
            result = summarize(
                mode="text",
                text=task.content,
                max_keywords=3,
                summary_len=deferred_input.summary_len,
                usage_task="parser.text.deferred",
            )
            return task.row_index, "text", result
    except Exception as exc:
        logger.warning(
            f"Deferred summary LLM call failed for idx={task.row_index}: {exc}"
        )
        return None

    logger.warning(f"Unknown deferred markdown summary task type: {type(task).__name__}")
    return None


def _apply_deferred_summary_results(
    deferred_input: MarkdownDeferredSummaryInput,
    results: list[DeferredResult | None],
) -> None:
    deferred_by_index = {task.row_index: task for task in deferred_input.tasks}

    for result in results:
        if result is None:
            continue

        row_index, task_type, task_result = result
        if task_type == "image":
            if not isinstance(task_result, AssetSummary):
                logger.warning(f"Invalid image deferred result for idx={row_index}")
                continue
            _apply_image_summary_result(
                deferred_input.rows,
                _get_image_task(deferred_by_index[row_index]),
                row_index,
                task_result,
            )
        elif task_type == "table":
            if not isinstance(task_result, AssetSummary):
                logger.warning(f"Invalid table deferred result for idx={row_index}")
                continue
            _apply_table_summary_result(
                deferred_input.rows,
                _get_table_task(deferred_by_index[row_index]),
                row_index,
                task_result,
            )
        elif task_type == "text":
            if not isinstance(task_result, BodySummary):
                logger.warning(f"Invalid text deferred result for idx={row_index}")
                continue
            apply_body_summary(deferred_input.rows[row_index], task_result)


def _get_image_task(task: MarkdownDeferredSummaryTask) -> ImageDeferredSummaryTask:
    if isinstance(task, ImageDeferredSummaryTask):
        return task
    raise TypeError(f"Expected image deferred task, got {type(task).__name__}")


def _get_table_task(task: MarkdownDeferredSummaryTask) -> TableDeferredSummaryTask:
    if isinstance(task, TableDeferredSummaryTask):
        return task
    raise TypeError(f"Expected table deferred task, got {type(task).__name__}")


def _apply_asset_result_preserving_index(
    row: list[str | int], result: AssetSummary
) -> None:
    """Write an asset result onto a markdown row, keeping the summary's index tag.

    Markdown image/table rows seed the summary column with an ``image-N`` /
    ``table-N`` index token on the first line (see ``image_asset`` /
    ``table_asset``). We preserve that token and append the generated summary
    below it, then write entities, asset_title, and the transitional keywords by
    name (audit §4.5).
    """
    while len(row) <= COL_ASSET_TITLE:
        row.append("")
    if result.summary:
        existing = str(row[COL_SUMMARY])
        index_token = existing.split("\n", 1)[0] if existing else ""
        row[COL_SUMMARY] = (
            f"{index_token}\n{result.summary}" if index_token else result.summary
        )
    row[COL_ENTITIES] = serialize_entities(result.entities)
    row[COL_KEYWORDS] = result.keywords_str()
    if result.title:
        row[COL_ASSET_TITLE] = result.title


def _apply_image_summary_result(
    rows: list[list[str | int]],
    original_task: ImageDeferredSummaryTask,
    row_index: int,
    result: AssetSummary,
) -> None:
    row = rows[row_index]
    _apply_asset_result_preserving_index(row, result)

    # Table-embedded images keep a stable filename so <img src> in
    # tables/*.html stays valid after summary generation.
    if not original_task.rename_file:
        return

    img_title = result.title
    if not img_title:
        return

    image_dir = original_task.image_dir
    old_img_name = original_task.image_name
    image_suffix = original_task.image_suffix
    cleaned_title = path_handle(str(img_title), mode="clean_single")
    if not isinstance(cleaned_title, str) or not cleaned_title:
        return
    safe_title = cleaned_title[:MAX_ASSET_FILE_NAME_CHARS]
    img_num_match = re.match(r"image-(\d+)", str(old_img_name))
    img_num = (
        img_num_match.group(1)
        if img_num_match
        else str(old_img_name).split("-")[1]
        if "-" in str(old_img_name)
        else "0"
    )
    new_img_name = path_handle(f"image-{img_num}-{safe_title}", mode="clean_single")
    if not isinstance(new_img_name, str) or not new_img_name:
        return
    old_path = os.path.join(image_dir, f"{old_img_name}{image_suffix}")
    new_path = os.path.join(image_dir, f"{new_img_name}{image_suffix}")
    if old_path == new_path or not os.path.exists(old_path):
        return

    os.rename(old_path, new_path)
    new_relative_path = f"images/{new_img_name}{image_suffix}"
    replace_chunk_ref_in_rows(rows, str(row[1]), new_relative_path)
    row[1] = new_relative_path


def _apply_table_summary_result(
    rows: list[list[str | int]],
    original_task: TableDeferredSummaryTask,
    row_index: int,
    result: AssetSummary,
) -> None:
    row = rows[row_index]
    _apply_asset_result_preserving_index(row, result)

    title = result.title
    if not title:
        return

    table_dir = original_task.table_dir
    old_table_name = original_task.table_name
    # Keep the original table-N index (same pattern as image rename).
    table_num_match = re.match(r"table-(\d+)", str(old_table_name))
    table_num = (
        table_num_match.group(1)
        if table_num_match
        else str(old_table_name).split("-")[1]
        if "-" in str(old_table_name)
        else "0"
    )
    safe_title = sanitize_table_name_from_header(str(title))
    new_table_name = path_handle(
        f"table-{table_num} {safe_title}", mode="clean_single"
    )
    old_path = os.path.join(table_dir, f"{old_table_name}.html")
    new_path = os.path.join(table_dir, f"{new_table_name}.html")
    if old_path == new_path or not os.path.exists(old_path):
        return

    os.rename(old_path, new_path)
    old_relative_path = str(row[1]) if len(row) > 1 else f"tables/{old_table_name}.html"
    new_relative_path = f"tables/{new_table_name}.html"
    replace_chunk_ref_in_rows(rows, old_relative_path, new_relative_path)
    row[0] = new_relative_path
    row[1] = new_relative_path
