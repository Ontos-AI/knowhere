"""
Page estimator for worker-side Document Ingestion billing.

Calculates page counts for billing based on:
- PDF: Physical page count from metadata
- PPTX: Slide count
- Text-based (DOC, DOCX, TXT, MD, JSON): Word-based estimation using count_cn_en
- Spreadsheet-based (XLS, XLSX, CSV, TSV): Visible non-empty cells, 1000 cells per page
"""

import math
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.services.document_parser.orchestration.format_router import (
    IMAGE_SOURCE_EXTENSIONS,
    TABLE_SOURCE_EXTENSIONS,
)
from shared.core.logging import logger
from shared.utils.text_utils import count_cn_en

WORDS_PER_PAGE = 500
CELLS_PER_PAGE = 1000


@dataclass(frozen=True)
class WorkloadEstimate:
    page_count: int
    method: str
    fallback_reason: str | None = None

    @property
    def used_fallback(self) -> bool:
        return self.fallback_reason is not None


class PageEstimator:
    """Estimate page count for billing purposes."""

    @classmethod
    def estimate(cls, file_path: str) -> int:
        """Estimate the billable page count for a file."""
        return cls.estimate_workload(file_path).page_count

    @classmethod
    def estimate_workload(cls, file_path: str) -> WorkloadEstimate:
        """Estimate billable workload while keeping fallback policy explicit."""
        path = Path(file_path)
        suffix = path.suffix.lower()

        if suffix == ".pdf":
            return cls._estimate_with_fallback("pdf_metadata", cls._count_pdf, file_path)
        if suffix == ".pptx":
            return cls._estimate_with_fallback("pptx_slides", cls._count_pptx, file_path)
        if suffix == ".doc":
            return cls._estimate_with_fallback("doc_conversion", cls._count_doc, file_path)
        if suffix == ".docx":
            return cls._estimate_with_fallback("docx_words", cls._count_docx, file_path)
        if suffix in TABLE_SOURCE_EXTENSIONS:
            method = "xls_conversion" if suffix == ".xls" else "xlsx_cells"
            estimator = (
                cls._count_xlsx if suffix == ".xlsx" else cls._count_spreadsheet
            )
            return cls._estimate_with_fallback(method, estimator, file_path)
        if suffix in [".txt", ".md", ".json", ".fragment"]:
            return cls._estimate_with_fallback("text_words", cls._count_text, file_path)
        if suffix in IMAGE_SOURCE_EXTENSIONS:
            return WorkloadEstimate(page_count=1, method="image_default")

        fallback_reason = f"unknown_file_type:{suffix or '<none>'}"
        logger.warning(
            f"Unknown file type for billing: {suffix}, defaulting to 1 page"
        )
        return WorkloadEstimate(
            page_count=1,
            method="unknown_file_type",
            fallback_reason=fallback_reason,
        )

    @classmethod
    def _estimate_with_fallback(
        cls,
        method: str,
        estimator: Callable[[str], int],
        file_path: str,
    ) -> WorkloadEstimate:
        try:
            page_count = max(1, estimator(file_path))
            return WorkloadEstimate(page_count=page_count, method=method)
        except ImportError as exc:
            fallback_reason = f"missing_dependency:{exc.name or exc}"
            logger.warning(
                f"Missing dependency for {method} page estimation, defaulting to 1 page: {exc}"
            )
        except Exception as exc:
            fallback_reason = f"{method}_error:{type(exc).__name__}"
            logger.error(f"Error estimating pages for {file_path}: {exc}")

        return WorkloadEstimate(
            page_count=1,
            method=method,
            fallback_reason=fallback_reason,
        )

    @classmethod
    def _count_pdf(cls, file_path: str) -> int:
        """Estimate pages for PDF using physical page count."""
        from pypdf import PdfReader

        reader = PdfReader(file_path)
        return len(reader.pages)

    @classmethod
    def _count_pptx(cls, file_path: str) -> int:
        """Estimate pages for PPTX using slide count."""
        from pptx import Presentation

        presentation = Presentation(file_path)
        return len(presentation.slides)

    @classmethod
    def _count_docx(cls, file_path: str) -> int:
        """Estimate pages for DOCX using word-based counting."""
        from app.services.document_parser.formats.docx.word_package import (
            open_word_document,
        )

        document = open_word_document(file_path)
        total_text = ""

        for paragraph in document.paragraphs:
            total_text += paragraph.text + " "

        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    total_text += cell.text + " "

        word_count = count_cn_en(total_text)
        return math.ceil(word_count / WORDS_PER_PAGE)

    @classmethod
    def _count_doc(cls, file_path: str) -> int:
        """Estimate pages for DOC by converting it to DOCX first."""
        from app.services.document_parser.conversion.legacy_converter import doc_to_docx

        with tempfile.TemporaryDirectory(prefix="page-estimator-doc-") as temp_dir:
            converted_path, _ = doc_to_docx(file_path, temp_dir)
            return cls._count_docx(converted_path)

    @classmethod
    def _count_xlsx(cls, file_path: str) -> int:
        """Estimate pages from visible non-empty cells, matching parse's hidden-sheet skip."""
        import openpyxl

        workbook = openpyxl.load_workbook(file_path, data_only=True)
        try:
            non_empty_cells = 0
            for sheet_name in workbook.sheetnames:
                worksheet = workbook[sheet_name]
                if worksheet.sheet_state != "visible":
                    continue
                if worksheet.max_row is None or worksheet.max_row == 0:
                    continue
                for row in worksheet.iter_rows(
                    min_row=1,
                    max_row=worksheet.max_row,
                    max_col=worksheet.max_column or 1,
                    values_only=True,
                ):
                    for value in row:
                        if value is None:
                            continue
                        non_empty_cells += 1
            return math.ceil(non_empty_cells / CELLS_PER_PAGE)
        finally:
            workbook.close()

    @classmethod
    def _count_spreadsheet(cls, file_path: str) -> int:
        """Normalize csv/tsv/xls to xlsx, then count visible non-empty cells."""
        from app.services.document_parser.formats.excel.normalize import (
            normalize_table_source,
        )

        source_path = Path(file_path)
        with tempfile.TemporaryDirectory(prefix="page-estimator-spreadsheet-") as temp_dir:
            work_xlsx = normalize_table_source(
                file_path,
                source_path.name,
                temp_dir,
            )
            return cls._count_xlsx(work_xlsx)

    @classmethod
    def _count_text(cls, file_path: str) -> int:
        """Estimate pages for text-like files using word-based counting."""
        with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
            content = file.read()

        word_count = count_cn_en(content)
        return math.ceil(word_count / WORDS_PER_PAGE)
