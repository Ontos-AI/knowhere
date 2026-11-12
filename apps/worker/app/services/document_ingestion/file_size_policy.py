from __future__ import annotations

from app.services.document_parser.orchestration.format_router import (
    MEDIA_SOURCE_EXTENSIONS,
)

from shared.core.config import settings


SUPPORT_EMAIL = "team@knowhereto.ai"
ISSUES_URL = "https://github.com/Ontos-AI/knowhere/issues"


def max_source_file_bytes(*, file_extension: str) -> int:
    if file_extension.lower() in MEDIA_SOURCE_EXTENSIONS:
        return settings.MAX_MEDIA_FILE_SIZE
    return settings.MAX_FILE_SIZE


def build_file_size_limit_message(*, limit_mb: int, file_extension: str) -> str:
    return (
        f"File size exceeds the {limit_mb}MB limit for "
        f"{file_extension or 'this file'}. For larger files, contact "
        f"{SUPPORT_EMAIL} or open an issue at {ISSUES_URL}."
    )
