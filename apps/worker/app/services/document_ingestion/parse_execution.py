from __future__ import annotations

from app.services.document_ingestion.processing_context import ParseJobContext
from app.services.document_ingestion.source_preparation import PreparedSourceFile
from app.services.document_parser import parse_service
from app.services.document_parser.orchestration.parse_output import ParseOutput
from app.services.document_parser.support.stage_profiler import (
    stage_timer,
    init_stage_tracker,
    cleanup_stage_tracker,
    get_current_stage_tracker,
)
from loguru import logger

from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.services.ai.token_tracking import (
    init_token_tracker,
    cleanup_token_tracker,
    get_current_token_tracker,
)


def execute_document_parse(
    *,
    job_id: str,
    job_context: ParseJobContext,
    prepared_source: PreparedSourceFile,
    output_dir: str,
) -> ParseOutput:
    """Run worker document parsing for a prepared local source file."""
    doc_type = JobMetadataHelper.get_parsing_param(
        job_context.job_metadata,
        "doc_type",
        "auto",
    )
    parse_track = JobMetadataHelper.get_parse_track(job_context.job_metadata)
    logger.info(
        f"Start parse: job_id={job_id}, "
        f"filename={prepared_source.source_file_name}, "
        f"internal_filename={prepared_source.internal_parse_name}, "
        f"type={doc_type}, parse_track={parse_track}"
    )

    token_usage_dict = get_current_token_tracker()
    owns_token_tracker = token_usage_dict is None
    if token_usage_dict is None:
        token_usage_dict = init_token_tracker()

    stage_timing_dict = get_current_stage_tracker()
    owns_stage_tracker = stage_timing_dict is None
    if stage_timing_dict is None:
        stage_timing_dict = init_stage_tracker()

    try:
        with stage_timer(
            "worker.parse.document",
            job_id=job_id,
            filename=prepared_source.source_file_name,
            doc_type=doc_type,
            parse_track=parse_track,
        ):
            if parse_track == "page_memory":
                parse_output = _execute_page_memory_parse(
                    job_id=job_id,
                    prepared_source=prepared_source,
                    output_dir=output_dir,
                )
            else:
                parse_output = parse_service.checkerboard_parse_output(
                    file_full_path=prepared_source.local_file_path,
                    filename=prepared_source.source_file_name,
                    output_dir=output_dir,
                    job_id=job_id,
                    internal_output_filename=prepared_source.internal_parse_name,
                    doc_type=doc_type,
                    smart_title_parse=JobMetadataHelper.get_parsing_param(
                        job_context.job_metadata,
                        "smart_title_parse",
                        True,
                    ),
                    summary_image=JobMetadataHelper.get_parsing_param(
                        job_context.job_metadata,
                        "summary_image",
                        True,
                    ),
                    summary_table=JobMetadataHelper.get_parsing_param(
                        job_context.job_metadata,
                        "summary_table",
                        True,
                    ),
                    summary_txt=JobMetadataHelper.get_parsing_param(
                        job_context.job_metadata,
                        "summary_txt",
                        True,
                    ),
                    add_frag_desc=JobMetadataHelper.get_parsing_param(
                        job_context.job_metadata,
                        "add_frag_desc",
                        "",
                    ),
                    s3_key=job_context.s3_key,
                )

        logger.info(
            "File parsing completed: "
            f"job_id={job_id}, output_dir={parse_output.output_dir}, "
            f"chunks={parse_output.rows_count}"
        )

        job_context.job_metadata["stages"] = {
            "timing_ms": dict(stage_timing_dict),
            "token_usage": dict(token_usage_dict),
        }
    finally:
        if owns_token_tracker:
            cleanup_token_tracker()
        if owns_stage_tracker:
            cleanup_stage_tracker()

    return parse_output


def _execute_page_memory_parse(
    *,
    job_id: str,
    prepared_source: PreparedSourceFile,
    output_dir: str,
) -> ParseOutput:
    from app.services.page_memory.memory_service import PageMemoryInput, run

    page_output_dir, parsed_df = run(
        PageMemoryInput(
            file_path=prepared_source.local_file_path,
            filename=prepared_source.source_file_name,
            internal_output_filename=prepared_source.internal_parse_name,
            output_dir=output_dir,
            job_id=job_id,
        )
    )
    return ParseOutput(output_dir=page_output_dir, parsed_df=parsed_df)
