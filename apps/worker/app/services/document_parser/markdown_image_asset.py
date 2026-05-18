from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.services.document_parser.identifiers import gen_str_codes
from app.services.document_parser.image_parser import perceptual_hash
from app.services.document_parser.inline_asset import build_image_asset_row
from app.services.document_parser.markdown_deferred_task import (
    ImageDeferredSummaryTask,
    MarkdownDeferredSummaryTask,
)
from app.services.document_parser.markdown_parse_state import ParserRowValues
from loguru import logger

from shared.utils.chunk_refs import build_chunk_ref
from shared.utils.file_utils import path_handle


@dataclass(frozen=True)
class MarkdownImageAsset:
    content_item: str | None
    row_values: ParserRowValues | None
    cache_key: str | None
    cache_entry: dict[str, str] | None
    deferred_task: MarkdownDeferredSummaryTask | None
    should_advance_image_count: bool


@dataclass(frozen=True)
class MarkdownImageAssetRequest:
    output_dir: str
    image_dir: str
    image_path: str
    image_name: str
    image_count: int
    last_context: str
    image_summary: str | None
    timestamp: str
    current_page_number: int
    seen_images: dict[str, dict[str, str]]
    summary_image: bool
    row_index: int


def build_markdown_image_asset(
    request: MarkdownImageAssetRequest,
) -> MarkdownImageAsset:
    image_suffix = os.path.splitext(request.image_path)[-1]
    source_path = resolve_markdown_image_source_path(
        request.output_dir,
        request.image_path,
    )
    if source_path is None or not source_path.exists():
        logger.warning(f"Image file not found, skipping rename: {request.image_path}")
        return _empty_asset(should_advance_image_count=True)

    with open(source_path, "rb") as image_file:
        image_binary_hash = perceptual_hash(image_file.read())

    if image_binary_hash in request.seen_images:
        return _build_duplicate_image_asset(
            source_path=source_path,
            cache_entry=request.seen_images[image_binary_hash],
            timestamp=request.timestamp,
            current_page_number=request.current_page_number,
        )

    relative_image_path = f"images/{request.image_name}{image_suffix}"
    target_image_path = os.path.join(
        request.image_dir,
        f"{request.image_name}{image_suffix}",
    )
    os.rename(source_path, target_image_path)

    image_index = f"image-{request.image_count}"
    effective_summary = request.image_summary or request.last_context or None
    image_summary_field = (
        f"{image_index}\n{effective_summary}" if effective_summary else image_index
    )
    image_content = _build_image_content(
        relative_image_path=relative_image_path,
        summary=effective_summary,
    )
    image_know_id = gen_str_codes(image_binary_hash)
    row_values = _build_image_row_values(
        content=image_content,
        relative_path=relative_image_path,
        summary=image_summary_field,
        know_id=image_know_id,
        timestamp=request.timestamp,
        current_page_number=request.current_page_number,
    )
    cache_entry = {
        "relative_img_path": relative_image_path,
        "img_content": image_content,
        "img_summary_field": image_summary_field,
        "temp_uid": image_know_id,
    }

    deferred_task = None
    if request.summary_image:
        deferred_task = ImageDeferredSummaryTask(
            row_index=request.row_index,
            relative_path=relative_image_path,
            image_dir=request.image_dir,
            image_name=request.image_name,
            image_suffix=image_suffix,
        )

    return MarkdownImageAsset(
        content_item=image_content,
        row_values=row_values,
        cache_key=image_binary_hash,
        cache_entry=cache_entry,
        deferred_task=deferred_task,
        should_advance_image_count=True,
    )


def build_markdown_image_name(*, image_count: int, last_context: str) -> str:
    image_name_context = path_handle(last_context[:10], mode="clean_single")
    return f"image-{str(image_count)}-{image_name_context}"


def resolve_workspace_image_path(
    candidate_path: Path, workspace_path: Path,
) -> Path | None:
    """Return the candidate only when it exists inside the current job workspace."""
    resolved_path = candidate_path.resolve(strict=False)
    try:
        resolved_path.relative_to(workspace_path)
    except ValueError:
        return None
    return resolved_path if resolved_path.exists() else None


def resolve_markdown_image_source_path(output_dir: str, image_path: str) -> Path | None:
    """Handle local absolute refs and container cwd-relative refs safely."""
    if not image_path:
        return None

    workspace_path = Path(output_dir).resolve()
    raw_path = Path(image_path).expanduser()
    candidate_paths = (
        [raw_path]
        if raw_path.is_absolute()
        else [
            workspace_path / raw_path,
            Path.cwd() / raw_path,
        ]
    )

    for candidate_path in candidate_paths:
        resolved_path = resolve_workspace_image_path(candidate_path, workspace_path)
        if resolved_path is not None:
            return resolved_path

    return None


def _build_duplicate_image_asset(
    *,
    source_path: Path,
    cache_entry: dict[str, str],
    timestamp: str,
    current_page_number: int,
) -> MarkdownImageAsset:
    row_values = _build_image_row_values(
        content=cache_entry["img_content"],
        relative_path=cache_entry["relative_img_path"],
        summary=cache_entry["img_summary_field"],
        know_id=cache_entry["temp_uid"],
        timestamp=timestamp,
        current_page_number=current_page_number,
    )
    try:
        source_path.unlink()
    except OSError:
        pass
    logger.debug("Skipped duplicate image")
    return MarkdownImageAsset(
        content_item=cache_entry["img_content"],
        row_values=row_values,
        cache_key=None,
        cache_entry=None,
        deferred_task=None,
        should_advance_image_count=False,
    )


def _build_image_content(*, relative_image_path: str, summary: str | None) -> str:
    image_reference = build_chunk_ref(relative_image_path)
    if summary:
        return f"\n{summary}\n{image_reference}\n"
    return f"\n{image_reference}\n"


def _build_image_row_values(
    *,
    content: str,
    relative_path: str,
    summary: str,
    know_id: str,
    timestamp: str,
    current_page_number: int,
) -> ParserRowValues:
    image_row = build_image_asset_row(
        content=content,
        relative_path=relative_path,
        summary=summary,
        know_id=know_id,
        addtime=timestamp,
        page_nums=str(current_page_number) if current_page_number > 0 else "",
    )
    return cast(ParserRowValues, image_row.to_list())


def _empty_asset(*, should_advance_image_count: bool) -> MarkdownImageAsset:
    return MarkdownImageAsset(
        content_item=None,
        row_values=None,
        cache_key=None,
        cache_entry=None,
        deferred_task=None,
        should_advance_image_count=should_advance_image_count,
    )
