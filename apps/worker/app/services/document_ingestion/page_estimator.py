"""
Page estimator for worker-side Document Ingestion billing.

Calculates page counts for billing based on:
- PDF: Physical page count from metadata
- PPTX: Slide count
- Text-based (DOC, DOCX, TXT, MD, JSON): Word-based estimation using count_cn_en
- Spreadsheet-based (XLS, XLSX): Row-based estimation
"""

import math
import tempfile
from pathlib import Path

from shared.core.logging import logger
from shared.utils.text_utils import count_cn_en

WORDS_PER_PAGE = 500
ROWS_PER_PAGE = 50


class PageEstimator:
    """Estimate page count for billing purposes."""

    @classmethod
    def estimate(cls, file_path: str) -> int:
        """Estimate the billable page count for a file."""
        path = Path(file_path)
        suffix = path.suffix.lower()

        try:
            if suffix == ".pdf":
                return cls._estimate_pdf(file_path)
            if suffix == ".pptx":
                return cls._estimate_pptx(file_path)
            if suffix == ".doc":
                return cls._estimate_doc(file_path)
            if suffix == ".docx":
                return cls._estimate_docx(file_path)
            if suffix == ".xls":
                return cls._estimate_xls(file_path)
            if suffix == ".xlsx":
                return cls._estimate_xlsx(file_path)
            if suffix in [".txt", ".md", ".json", ".fragment"]:
                return cls._estimate_text(file_path)
            if suffix in [".png", ".jpg", ".jpeg"]:
                return 1

            logger.warning(
                f"Unknown file type for billing: {suffix}, defaulting to 1 page"
            )
            return 1
        except Exception as exc:
            logger.error(f"Error estimating pages for {file_path}: {exc}")
            return 1

    @classmethod
    def _estimate_pdf(cls, file_path: str) -> int:
        """Estimate pages for PDF using physical page count."""
        try:
            from pypdf import PdfReader

            reader = PdfReader(file_path)
            return max(1, len(reader.pages))
        except ImportError:
            logger.warning("pypdf not installed, defaulting to 1 page")
            return 1
        except Exception as exc:
            logger.error(f"PDF estimation error: {exc}")
            return 1

    @classmethod
    def _estimate_pptx(cls, file_path: str) -> int:
        """Estimate pages for PPTX using slide count."""
        try:
            from pptx import Presentation

            presentation = Presentation(file_path)
            return max(1, len(presentation.slides))
        except ImportError:
            logger.warning("python-pptx not installed, defaulting to 1 page")
            return 1
        except Exception as exc:
            logger.error(f"PPTX estimation error: {exc}")
            return 1

    @classmethod
    def _estimate_docx(cls, file_path: str) -> int:
        """Estimate pages for DOCX using word-based counting."""
        try:
            from docx import Document

            document = Document(file_path)
            total_text = ""

            for paragraph in document.paragraphs:
                total_text += paragraph.text + " "

            for table in document.tables:
                for row in table.rows:
                    for cell in row.cells:
                        total_text += cell.text + " "

            word_count = count_cn_en(total_text)
            return max(1, math.ceil(word_count / WORDS_PER_PAGE))
        except ImportError:
            logger.warning("python-docx not installed")
            return 1
        except Exception as exc:
            logger.error(f"DOCX estimation error: {exc}")
            return 1

    @classmethod
    def _estimate_doc(cls, file_path: str) -> int:
        """Estimate pages for DOC by converting it to DOCX first."""
        try:
            from app.services.document_parser.legacy_converter import doc_to_docx

            with tempfile.TemporaryDirectory(prefix="page-estimator-doc-") as temp_dir:
                converted_path, _ = doc_to_docx(file_path, temp_dir)
                return cls._estimate_docx(converted_path)
        except Exception as exc:
            logger.error(f"DOC estimation error: {exc}")
            return 1

    @classmethod
    def _estimate_xlsx(cls, file_path: str) -> int:
        """Estimate pages for XLSX using row count."""
        try:
            import pandas as pd

            workbook = pd.ExcelFile(file_path)
            total_rows = 0

            for sheet_name in workbook.sheet_names:
                dataframe = pd.read_excel(workbook, sheet_name=sheet_name)
                total_rows += len(dataframe)

            return max(1, math.ceil(total_rows / ROWS_PER_PAGE))
        except ImportError:
            logger.warning("pandas not installed")
            return 1
        except Exception as exc:
            logger.error(f"XLSX estimation error: {exc}")
            return 1

    @classmethod
    def _estimate_xls(cls, file_path: str) -> int:
        """Estimate pages for XLS by converting it to XLSX first."""
        try:
            from app.services.document_parser.legacy_converter import xls_to_xlsx

            with tempfile.TemporaryDirectory(prefix="page-estimator-xls-") as temp_dir:
                converted_path, _ = xls_to_xlsx(file_path, temp_dir)
                return cls._estimate_xlsx(converted_path)
        except Exception as exc:
            logger.error(f"XLS estimation error: {exc}")
            return 1

    @classmethod
    def _estimate_text(cls, file_path: str) -> int:
        """Estimate pages for text-like files using word-based counting."""
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
                content = file.read()

            word_count = count_cn_en(content)
            return max(1, math.ceil(word_count / WORDS_PER_PAGE))
        except Exception as exc:
            logger.error(f"Text estimation error: {exc}")
            return 1
