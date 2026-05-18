"""Stable parser seam backed by dedicated orchestration modules."""

import pandas as pd

from app.services.document_parser.orchestration.parse_input import ParseInput, ParseOptions
from app.services.document_parser.orchestration.parse_pipeline import run_parse_pipeline


def checkerboard_inject_parse(
    file_full_path: str,
    filename: str,
    output_dir: str,
    internal_output_filename: str,
    job_id: str | None = None,
    kb_dir: str = "Default_Root",
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
) -> tuple[str, pd.DataFrame | None]:
    """Run the stable parser seam using dedicated orchestration modules."""
    parse_input = ParseInput(
        file_full_path=file_full_path,
        filename=filename,
        internal_output_filename=internal_output_filename,
        job_id=job_id,
        kb_dir=kb_dir,
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
    return run_parse_pipeline(parse_input).as_legacy_tuple()
