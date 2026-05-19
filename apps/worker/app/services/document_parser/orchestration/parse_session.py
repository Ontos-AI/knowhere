from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from app.services.document_parser.formats.atlas.classifier import classify_atlas_with_vlm
from app.services.document_parser.orchestration.path_segment import (
    build_parser_path_segment,
)
from app.services.document_parser.orchestration.parse_input import ParseInput
from app.services.document_parser.profiling.doc_profiler import profile_document
from app.services.document_parser.support.stage_profiler import stage_timer
from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import ValidationException


@dataclass(frozen=True)
class ParseSession:
    base_llm_paras: dict[str, object]
    base_url: str
    file_full_path: str
    filename: str
    fragment_content: str
    full_output_dir: str
    internal_output_filename: str
    job_id: str | None
    output_dir: str
    profile: Any
    relative_root: str
    s3_key: str | None

    @classmethod
    def from_input(
        cls,
        *,
        parse_input: ParseInput,
        base_llm_paras: dict[str, object],
        full_output_dir: str,
        profile: Any,
        relative_root: str,
    ) -> "ParseSession":
        return cls(
            base_llm_paras=base_llm_paras,
            base_url=parse_input.base_url,
            file_full_path=parse_input.file_full_path,
            filename=parse_input.filename,
            fragment_content=parse_input.fragment_content,
            full_output_dir=full_output_dir,
            internal_output_filename=parse_input.internal_output_filename,
            job_id=parse_input.job_id,
            output_dir=parse_input.output_dir,
            profile=profile,
            relative_root=relative_root,
            s3_key=parse_input.s3_key,
        )


def build_parse_session(parse_input: ParseInput) -> ParseSession:
    """Build the parser routing session from explicit parse inputs."""
    parse_options = parse_input.options
    base_llm_paras = {
        "llm_histories": parse_options.llm_histories,
        "smart_title_parse": parse_options.smart_title_parse,
        "summary_image": parse_options.summary_image,
        "summary_table": parse_options.summary_table,
        "summary_txt": parse_options.summary_txt,
        "stopwords": parse_options.stopwords,
        "doc_type": parse_options.doc_type,
        "frag_desc": parse_options.add_frag_desc,
        "model_name": settings.NORMOL_MODEL,
        "hierarchy_model_name": settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL,
    }

    logger.debug(f"baseurl: {parse_input.base_url}")
    logger.debug(f"file_full_path: {parse_input.file_full_path}")

    relative_root, full_output_dir = _resolve_output_paths(
        filename=parse_input.filename,
        internal_output_filename=parse_input.internal_output_filename,
        output_dir=parse_input.output_dir,
    )
    logger.debug(f"relative_root: {relative_root}")
    logger.debug(f"full_output_dir: {full_output_dir}")

    with stage_timer("document.profile", filename=parse_input.filename):
        profile = profile_document(
            parse_input.file_full_path,
            parse_input.internal_output_filename,
        )
    logger.info(f"📋 DocProfile: {profile.summary()}")
    logger.debug(f"📋 Reasoning: {profile.reasoning}")

    if profile.atlas_candidate and profile.doc_category not in ("atlas", "ppt_converted"):
        logger.info(
            f"🔍 Atlas candidate detected, running VLM visual check for {parse_input.filename}"
        )
        with stage_timer("document.atlas_vlm_check", filename=parse_input.filename):
            vlm_is_atlas = classify_atlas_with_vlm(parse_input.file_full_path)
        if vlm_is_atlas:
            profile.doc_category = "atlas"
            profile.reasoning += " | vlm_confirmed_atlas=True"
            logger.info(f"✅ VLM confirmed atlas for {parse_input.filename}")
        else:
            profile.reasoning += " | vlm_confirmed_atlas=False"
            logger.info(
                f"ℹ️ VLM rejected atlas for {parse_input.filename}, routing as generic"
            )

    pdf_page_limit = settings.MAX_PDF_PAGE_LIMIT
    if profile.file_type == "pdf" and profile.page_count > pdf_page_limit:
        raise ValidationException(
            user_message=(
                f"Document too large: {profile.page_count} pages exceeds the {pdf_page_limit}-page limit. "
                "Please split the document and upload in smaller batches."
            ),
            violations=[
                {
                    "field": "page_count",
                    "description": f"PDF has {profile.page_count} pages, limit is {pdf_page_limit}",
                }
            ],
        )

    if profile.doc_category == "atlas":
        filename, internal_output_filename, relative_root, full_output_dir = (
            _rename_atlas_output(
                filename=parse_input.filename,
                internal_output_filename=parse_input.internal_output_filename,
                output_dir=parse_input.output_dir,
            )
        )
        logger.info(f"📐 Atlas output renamed: {filename}")
        parse_input = ParseInput(
            file_full_path=parse_input.file_full_path,
            filename=filename,
            output_dir=parse_input.output_dir,
            internal_output_filename=internal_output_filename,
            job_id=parse_input.job_id,
            options=parse_input.options,
            base_url=parse_input.base_url,
            fragment_content=parse_input.fragment_content,
            s3_key=parse_input.s3_key,
        )

    return ParseSession.from_input(
        parse_input=parse_input,
        base_llm_paras=base_llm_paras,
        full_output_dir=full_output_dir,
        profile=profile,
        relative_root=relative_root,
    )


def _rename_atlas_output(
    *,
    filename: str,
    internal_output_filename: str,
    output_dir: str,
) -> tuple[str, str, str, str]:
    name_base, _ = os.path.splitext(filename)
    internal_name_base, _ = os.path.splitext(internal_output_filename)
    atlas_filename = name_base + ".atlas"
    atlas_internal_filename = internal_name_base + ".atlas"
    relative_root, full_output_dir = _resolve_output_paths(
        filename=atlas_filename,
        internal_output_filename=atlas_internal_filename,
        output_dir=output_dir,
    )
    return atlas_filename, atlas_internal_filename, relative_root, full_output_dir


def _resolve_output_paths(
    *,
    filename: str,
    internal_output_filename: str,
    output_dir: str,
) -> tuple[str, str]:
    filename_segment = build_parser_path_segment(filename)
    internal_filename_segment = build_parser_path_segment(
        internal_output_filename,
        default=filename_segment,
    )
    relative_root = filename_segment

    full_output_dir = os.path.join(
        output_dir,
        internal_filename_segment,
    )
    resolved_output_dir = os.path.realpath(output_dir)
    resolved_full_output_dir = os.path.realpath(full_output_dir)
    if (
        os.path.commonpath([resolved_output_dir, resolved_full_output_dir])
        != resolved_output_dir
    ):
        raise ValueError(
            f"Parser output directory escaped task workspace: {full_output_dir}"
        )
    os.makedirs(resolved_full_output_dir, exist_ok=True)

    logger.debug(f"internal_output_root: {internal_filename_segment}")
    return relative_root, resolved_full_output_dir
