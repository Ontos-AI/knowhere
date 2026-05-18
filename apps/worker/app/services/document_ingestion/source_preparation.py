from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from app.services.document_ingestion.processing_context import ParseJobContext
from app.services.document_ingestion.workspace import download_s3_file_to_temp
from app.services.document_parser.support.internal_parse_name import (
    prepare_internal_parse_input,
)
from loguru import logger

from shared.models.schemas.job_metadata import JobMetadataHelper

DownloadSourceFile = Callable[[str, str, str], str]


@dataclass(frozen=True)
class PreparedSourceFile:
    source_file_name: str
    internal_parse_name: str
    local_file_path: str
    file_extension: str


def prepare_source_file(
    *,
    job_id: str,
    job_context: ParseJobContext,
    input_dir: str,
    download_source_file: DownloadSourceFile = download_s3_file_to_temp,
) -> PreparedSourceFile:
    """Download and normalize the source file before parser execution."""
    source_file_name = JobMetadataHelper.get_source_file_name(
        job_context.job_metadata,
    ) or os.path.basename(job_context.s3_key)
    file_extension = (
        os.path.splitext(job_context.s3_key)[1].lower() if job_context.s3_key else ""
    )

    local_file_path = download_source_file(
        job_context.s3_key,
        file_extension,
        input_dir,
    )
    logger.info(f"File downloaded: job_id={job_id}, local_path={local_file_path}")

    prepared_parse_input = prepare_internal_parse_input(
        local_file_path,
        source_file_name,
        fallback_ext=file_extension,
        prefer_fallback_ext=True,
    )
    logger.info(
        f"File prepared for parsing: job_id={job_id}, "
        f"internal_filename={prepared_parse_input.internal_filename}, "
        f"local_path={prepared_parse_input.file_path}"
    )

    return PreparedSourceFile(
        source_file_name=source_file_name,
        internal_parse_name=prepared_parse_input.internal_filename,
        local_file_path=prepared_parse_input.file_path,
        file_extension=file_extension,
    )
