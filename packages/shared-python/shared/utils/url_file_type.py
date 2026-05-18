"""Compatibility wrapper for shared.services.http.url_file_type."""

from shared.services.http.url_file_type import (
    CONTENT_TYPE_TO_EXTENSION,
    MAX_SAFE_REDIRECTS,
    REDIRECT_STATUS_CODES,
    URL_VALIDATION_DESCRIPTIONS,
    resolve_file_extension_async,
    resolve_file_extension_sync,
)

__all__ = [
    "CONTENT_TYPE_TO_EXTENSION",
    "MAX_SAFE_REDIRECTS",
    "REDIRECT_STATUS_CODES",
    "URL_VALIDATION_DESCRIPTIONS",
    "resolve_file_extension_async",
    "resolve_file_extension_sync",
]
