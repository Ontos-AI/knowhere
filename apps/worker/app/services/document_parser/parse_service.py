"""Stable parser seam backed by dedicated orchestration modules."""

from app.services.document_parser.orchestration.parse_input import ParseInput, ParseOptions
from app.services.document_parser.orchestration.parse_pipeline import (
    ParsePipelineResult,
    run_parse_pipeline,
)


def checkerboard_parse_output(
    file_full_path: str,
    filename: str,
    output_dir: str,
    internal_output_filename: str,
    job_id: str | None = None,
    llm_histories: int = 5,
    smart_title_parse: bool = True,
    summary_image: bool = True,
    summary_table: bool = True,
    summary_txt: bool = True,
    stopwords: list[str] | None = None,
    doc_type: str = "auto",
    add_frag_desc: str = "",
    base_url: str = "",
    fragment_content: str = "",
    s3_key: str | None = None,
) -> ParsePipelineResult:
    """Run the stable parser seam and return the parser output contract."""
    parse_input = ParseInput(
        file_full_path=file_full_path,
        filename=filename,
        internal_output_filename=internal_output_filename,
        job_id=job_id,
        output_dir=output_dir,
        options=ParseOptions(
            add_frag_desc=add_frag_desc,
            doc_type=doc_type,
            llm_histories=llm_histories,
            smart_title_parse=smart_title_parse,
            stopwords=stopwords,
            summary_image=summary_image,
            summary_table=summary_table,
            summary_txt=summary_txt,
        ),
        base_url=base_url,
        fragment_content=fragment_content,
        s3_key=s3_key,
    )
    return run_parse_pipeline(parse_input)
