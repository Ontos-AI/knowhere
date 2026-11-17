from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.services.document_parser.orchestration.parse_output import ParseOutput
from app.services.document_parser.orchestration.parse_session import ParseSession


class DocumentParseAdapter(Protocol):
    @property
    def document_format(self) -> object:
        """Document format handled by this adapter."""

    def parse(self, session: ParseSession) -> ParseOutput:
        """Parse a document session into a stable parser output object."""
        raise NotImplementedError


@dataclass(frozen=True)
class TextParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> ParseOutput:
        from app.services.document_parser.formats.markdown.parser import parse_md
        from app.services.document_parser.formats.text.parser import parse_texts

        text_lines = parse_texts(file_path=session.file_full_path)
        parsed_df = parse_md(
            session.full_output_dir,
            source_type="md",
            md_lines=text_lines,
            base_llm_paras=session.base_llm_paras,
            relative_root=session.relative_root,
        )
        return ParseOutput(output_dir=session.full_output_dir, parsed_df=parsed_df)


@dataclass(frozen=True)
class ImageParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> ParseOutput:
        from app.services.document_parser.formats.image.parser import parse_image

        parsed_df = parse_image(
            session.file_full_path,
            filename=session.filename,
            output_dir=session.full_output_dir,
            base_llm_paras=session.base_llm_paras,
            relative_root=session.relative_root,
        )
        return ParseOutput(output_dir=session.full_output_dir, parsed_df=parsed_df)


@dataclass(frozen=True)
class DocParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> ParseOutput:
        from app.services.document_parser.conversion.legacy_converter import doc_to_docx

        converted_docx_path, _ = doc_to_docx(
            session.file_full_path,
            outdir=session.full_output_dir,
        )
        return _parse_docx_path(converted_docx_path, session)


@dataclass(frozen=True)
class DocxParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> ParseOutput:
        return _parse_docx_path(session.file_full_path, session)


@dataclass(frozen=True)
class SpreadsheetParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> ParseOutput:
        from app.services.document_parser.formats.excel.normalize import (
            normalize_table_source,
        )

        work_xlsx = normalize_table_source(
            session.file_full_path,
            session.filename,
            session.full_output_dir,
        )
        return _parse_xlsx_path(work_xlsx, session)


@dataclass(frozen=True)
class MarkdownParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> ParseOutput:
        from app.services.document_parser.formats.markdown.parser import parse_md

        parsed_df = parse_md(
            session.full_output_dir,
            source_type="md",
            file_path=session.file_full_path,
            base_llm_paras=session.base_llm_paras,
            relative_root=session.relative_root,
        )
        return ParseOutput(output_dir=session.full_output_dir, parsed_df=parsed_df)


@dataclass(frozen=True)
class HtmlParseAdapter:
    """Adapter that parses .html / .htm files through the markdown pipeline."""
    document_format: object

    def parse(self, session: ParseSession) -> ParseOutput:
        from app.services.document_parser.formats.html.document_parser import parse_html

        parsed_df = parse_html(
            session.full_output_dir,
            source_type="html",
            file_path=session.file_full_path,
            base_llm_paras=session.base_llm_paras,
            relative_root=session.relative_root,
        )
        return ParseOutput(output_dir=session.full_output_dir, parsed_df=parsed_df)


@dataclass(frozen=True)
class MediaParseAdapter:
    document_format: object

    def parse(self, session: ParseSession) -> ParseOutput:
        from app.services.document_parser.formats.media.parser import parse_media

        return parse_media(
            session.file_full_path,
            filename=session.filename,
            output_dir=session.full_output_dir,
            relative_root=session.relative_root,
            chapterize=True,
            job_id=session.job_id,
        )


def _parse_docx_path(
    docx_path: str,
    session: ParseSession,
) -> ParseOutput:
    from app.services.document_parser.formats.docx.parser import convert_doc2dics, parse_docx

    parsed_structure, dataframe_list = parse_docx(
        docx_path,
        session.base_llm_paras,
        session.full_output_dir,
        session.filename,
        relative_root=session.relative_root,
    )
    parsed_df = convert_doc2dics(
        parsed_structure,
        dataframe_list,
        session.full_output_dir,
        base_llm_paras=session.base_llm_paras,
        relative_root=session.relative_root,
    )
    return ParseOutput(output_dir=session.full_output_dir, parsed_df=parsed_df)


def _parse_xlsx_path(
    xlsx_path: str,
    session: ParseSession,
) -> ParseOutput:
    from app.services.document_parser.formats.excel.table_parser import parse_xlsx

    parsed_df = parse_xlsx(
        xlsx_path,
        session.filename,
        session.full_output_dir,
        base_llm_paras=session.base_llm_paras,
        relative_root=session.relative_root,
    )
    return ParseOutput(output_dir=session.full_output_dir, parsed_df=parsed_df)
