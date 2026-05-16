from __future__ import annotations

import os

import pandas as pd

from app.services.document_parser.orchestration.parse_session import ParseSession
from shared.core.exceptions.domain_exceptions import ValidationException

SUPPORTED_FILE_TYPES: tuple[str, ...] = (
    ".txt",
    ".fragment",
    ".png",
    ".jpg",
    ".jpeg",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".pptx",
    ".md",
    ".json",
)


def route_document_parse(session: ParseSession) -> tuple[str, pd.DataFrame | None]:
    """Route a parser session to the correct adapter and return its output."""
    file_path_lower = session.file_full_path.lower()

    if ".fragment" in file_path_lower:
        from app.services.document_parser.fragment_parser import parse_fragment

        full_output_dir, _relative_root, parsed_df = parse_fragment(
            session.fragment_content,
            filename=session.filename,
            output_dir=session.output_dir,
            kb_dir=session.kb_dir,
            base_llm_paras=session.base_llm_paras,
        )
        return full_output_dir, parsed_df

    if ".txt" in file_path_lower:
        from app.services.document_parser.md_parser import parse_md
        from app.services.document_parser.txt_parser import parse_texts

        text_lines = parse_texts(file_path=session.file_full_path, baseurl=session.base_url)
        parsed_df = parse_md(
            session.full_output_dir,
            source_type="md",
            md_lines=text_lines,
            base_llm_paras=session.base_llm_paras,
            relative_root=session.relative_root,
        )
        return session.full_output_dir, parsed_df

    if any(extension in file_path_lower for extension in (".png", ".jpg", ".jpeg")):
        from app.services.document_parser.image_parser import parse_image

        parsed_df = parse_image(
            session.file_full_path,
            filename=session.filename,
            output_dir=session.full_output_dir,
            baseurl=session.base_url,
            base_llm_paras=session.base_llm_paras,
            relative_root=session.relative_root,
        )
        return session.full_output_dir, parsed_df

    if ".pdf" in file_path_lower:
        from app.services.document_parser.pdf_parser import parse_pdfs

        parsed_df = parse_pdfs(
            session.file_full_path,
            filename=session.filename,
            output_dir=session.full_output_dir,
            base_llm_paras=session.base_llm_paras,
            profile=session.profile,
            relative_root=session.relative_root,
            s3_key=session.s3_key,
        )
        return session.full_output_dir, parsed_df

    if ".doc" in file_path_lower and ".docx" not in file_path_lower:
        from app.services.document_parser.doc_parser import convert_doc2dics, parse_docx
        from app.services.document_parser.legacy_converter import doc_to_docx

        converted_docx_path, _ = doc_to_docx(
            session.file_full_path,
            outdir=session.full_output_dir,
        )
        parsed_structure, dataframe_list = parse_docx(
            converted_docx_path,
            session.base_llm_paras,
            session.full_output_dir,
            session.filename,
            session.base_url,
            relative_root=session.relative_root,
        )
        parsed_df = convert_doc2dics(
            parsed_structure,
            dataframe_list,
            session.full_output_dir,
            base_llm_paras=session.base_llm_paras,
            relative_root=session.relative_root,
        )
        return session.full_output_dir, parsed_df

    if ".docx" in file_path_lower:
        from app.services.document_parser.doc_parser import convert_doc2dics, parse_docx

        parsed_structure, dataframe_list = parse_docx(
            session.file_full_path,
            session.base_llm_paras,
            session.full_output_dir,
            session.filename,
            session.base_url,
            relative_root=session.relative_root,
        )
        parsed_df = convert_doc2dics(
            parsed_structure,
            dataframe_list,
            session.full_output_dir,
            base_llm_paras=session.base_llm_paras,
            relative_root=session.relative_root,
        )
        return session.full_output_dir, parsed_df

    if ".xls" in file_path_lower and ".xlsx" not in file_path_lower:
        from app.services.document_parser.legacy_converter import xls_to_xlsx
        from app.services.document_parser.table_parser import parse_xlsx

        converted_xlsx_path, _ = xls_to_xlsx(
            session.file_full_path,
            outdir=session.full_output_dir,
        )
        parsed_df = parse_xlsx(
            converted_xlsx_path,
            session.filename,
            session.full_output_dir,
            session.base_url,
            base_llm_paras=session.base_llm_paras,
            relative_root=session.relative_root,
        )
        return session.full_output_dir, parsed_df

    if ".xlsx" in file_path_lower:
        from app.services.document_parser.table_parser import parse_xlsx

        parsed_df = parse_xlsx(
            session.file_full_path,
            session.filename,
            session.full_output_dir,
            session.base_url,
            base_llm_paras=session.base_llm_paras,
            relative_root=session.relative_root,
        )
        return session.full_output_dir, parsed_df

    if ".pptx" in file_path_lower:
        from app.services.document_parser.pptx_parser import parse_pptx

        parsed_df = parse_pptx(
            session.file_full_path,
            filename=session.filename,
            output_dir=session.full_output_dir,
            base_llm_paras=session.base_llm_paras,
            strategy="to_pdf_api",
            job_id=session.job_id,
            relative_root=session.relative_root,
            baseurl=session.base_url,
        )
        return session.full_output_dir, parsed_df

    if ".md" in file_path_lower:
        from app.services.document_parser.md_parser import parse_md

        parsed_df = parse_md(
            session.full_output_dir,
            source_type="md",
            file_path=session.file_full_path,
            base_llm_paras=session.base_llm_paras,
            relative_root=session.relative_root,
        )
        return session.full_output_dir, parsed_df

    if ".json" in file_path_lower:
        return session.full_output_dir, None

    file_ext = os.path.splitext(session.file_full_path)[1].lower()
    raise ValidationException(
        user_message=f"Unsupported file type: {file_ext}",
        violations=[
            {
                "field": "file_type",
                "description": f"Must be one of: {', '.join(SUPPORTED_FILE_TYPES)}",
            }
        ],
    )
