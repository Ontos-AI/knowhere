"""Stable parser seam backed by dedicated orchestration modules."""

import pandas as pd

from app.services.document_parser.orchestration.parse_session import build_parse_session
from app.services.document_parser.orchestration.postprocess import apply_parse_postprocess
from app.services.document_parser.orchestration.route_parse import route_document_parse


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
    session = build_parse_session(
        add_frag_desc=add_frag_desc,
        base_url=base_url,
        doc_type=doc_type,
        file_full_path=file_full_path,
        filename=filename,
        fragment_content=fragment_content,
        internal_output_filename=internal_output_filename,
        job_id=job_id,
        kb_dir=kb_dir,
        llm_histories=llm_histories,
        output_dir=output_dir,
        s3_key=s3_key,
        smart_title_parse=smart_title_parse,
        stopwords=stopwords,
        summary_image=summary_image,
        summary_table=summary_table,
        summary_txt=summary_txt,
    )
    full_output_dir, parsed_df = route_document_parse(session)
    parsed_df = apply_parse_postprocess(full_output_dir, parsed_df)
    return full_output_dir, parsed_df
