from __future__ import annotations

from app.services.document_ingestion.processing_context import ParseJobContext
from app.services.document_ingestion.source_preparation import PreparedSourceFile
from app.services.document_parser import parse_service
from app.services.document_parser.orchestration.parse_output import ParseOutput
from app.services.document_parser.support.stage_profiler import stage_timer
from loguru import logger

from shared.models.schemas.job_metadata import JobMetadataHelper


def execute_document_parse(
    *,
    job_id: str,
    job_context: ParseJobContext,
    prepared_source: PreparedSourceFile,
    output_dir: str,
) -> ParseOutput:
    """Run the parser adapter for a prepared source file."""
    doc_type = JobMetadataHelper.get_parsing_param(
        job_context.job_metadata,
        "doc_type",
        "auto",
    )
    logger.info(
        f"Start parse: job_id={job_id}, "
        f"filename={prepared_source.source_file_name}, "
        f"internal_filename={prepared_source.internal_parse_name}, type={doc_type}"
    )

    with stage_timer(
        "worker.parse.document",
        job_id=job_id,
        filename=prepared_source.source_file_name,
        doc_type=doc_type,
    ):
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
    return parse_output
