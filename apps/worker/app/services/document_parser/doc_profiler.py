"""
Agentic Document Profiler

Before data enters the pipeline, use lightweight analysis (~50ms) to generate
DocProfile, driving routing decisions and type annotations.

Usage:
    from app.services.document_parser.doc_profiler import profile_document
    profile = profile_document("/path/to/file.pdf")
"""

import os

from app.services.document_parser.doc_profile_model import DocProfile
from app.services.document_parser.doc_profile_pdf import profile_pdf


def profile_document(file_path: str, filename: str = "") -> DocProfile:
    """
    General document profiling entry point.

    Args:
        file_path: Local file path
        filename: File name (used to infer type)

    Returns:
        DocProfile
    """
    if not filename:
        filename = os.path.basename(file_path)

    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        return profile_pdf(file_path)

    return DocProfile(
        file_type=ext.lstrip("."),
        route="standard",
        decision_band="safe_standard",
        reasoning=f"Non-PDF format ({ext}), using default route",
    )
