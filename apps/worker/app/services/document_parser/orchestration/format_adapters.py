from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from app.services.document_parser.orchestration.parse_session import ParseSession


class DocumentParseAdapter(Protocol):
    @property
    def document_format(self) -> object:
        """Document format handled by this adapter."""

    def parse(self, session: ParseSession) -> tuple[str, pd.DataFrame | None]:
        """Parse a document session into the parser output directory and DataFrame."""
        ...


@dataclass(frozen=True)
class FragmentParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> tuple[str, pd.DataFrame | None]:
        from app.services.document_parser.fragment_parser import parse_fragment

        full_output_dir, _relative_root, parsed_df = parse_fragment(
            session.fragment_content,
            filename=session.filename,
            output_dir=session.output_dir,
            kb_dir=session.kb_dir,
            base_llm_paras=session.base_llm_paras,
        )
        return full_output_dir, parsed_df


@dataclass(frozen=True)
class TextParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> tuple[str, pd.DataFrame | None]:
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


@dataclass(frozen=True)
class ImageParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> tuple[str, pd.DataFrame | None]:
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


@dataclass(frozen=True)
class PdfParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> tuple[str, pd.DataFrame | None]:
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


@dataclass(frozen=True)
class DocParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> tuple[str, pd.DataFrame | None]:
        from app.services.document_parser.legacy_converter import doc_to_docx

        converted_docx_path, _ = doc_to_docx(
            session.file_full_path,
            outdir=session.full_output_dir,
        )
        return _parse_docx_path(converted_docx_path, session)


@dataclass(frozen=True)
class DocxParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> tuple[str, pd.DataFrame | None]:
        return _parse_docx_path(session.file_full_path, session)


@dataclass(frozen=True)
class XlsParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> tuple[str, pd.DataFrame | None]:
        from app.services.document_parser.legacy_converter import xls_to_xlsx

        converted_xlsx_path, _ = xls_to_xlsx(
            session.file_full_path,
            outdir=session.full_output_dir,
        )
        return _parse_xlsx_path(converted_xlsx_path, session)


@dataclass(frozen=True)
class XlsxParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> tuple[str, pd.DataFrame | None]:
        return _parse_xlsx_path(session.file_full_path, session)


@dataclass(frozen=True)
class PptxParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> tuple[str, pd.DataFrame | None]:
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


@dataclass(frozen=True)
class MarkdownParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> tuple[str, pd.DataFrame | None]:
        from app.services.document_parser.md_parser import parse_md

        parsed_df = parse_md(
            session.full_output_dir,
            source_type="md",
            file_path=session.file_full_path,
            base_llm_paras=session.base_llm_paras,
            relative_root=session.relative_root,
        )
        return session.full_output_dir, parsed_df


@dataclass(frozen=True)
class JsonParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> tuple[str, pd.DataFrame | None]:
        return session.full_output_dir, None


def _parse_docx_path(
    docx_path: str,
    session: ParseSession,
) -> tuple[str, pd.DataFrame | None]:
    from app.services.document_parser.doc_parser import convert_doc2dics, parse_docx

    parsed_structure, dataframe_list = parse_docx(
        docx_path,
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


def _parse_xlsx_path(
    xlsx_path: str,
    session: ParseSession,
) -> tuple[str, pd.DataFrame | None]:
    from app.services.document_parser.excel_table_parser import parse_xlsx

    parsed_df = parse_xlsx(
        xlsx_path,
        session.filename,
        session.full_output_dir,
        session.base_url,
        base_llm_paras=session.base_llm_paras,
        relative_root=session.relative_root,
    )
    return session.full_output_dir, parsed_df
