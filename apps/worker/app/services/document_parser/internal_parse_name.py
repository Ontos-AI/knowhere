# pyright: reportArgumentType=false, reportCallIssue=false
"""Helpers for internal parser filenames."""

import os
from dataclasses import dataclass

from shared.utils.file_utils import path_handle


@dataclass(frozen=True)
class PreparedInternalParseInput:
    """Internal parser input prepared with a normalized filename and on-disk path."""

    internal_filename: str
    file_path: str


def normalize_internal_parse_name(
    filename: str | None,
    fallback_ext: str = "",
    prefer_fallback_ext: bool = False,
) -> str:
    """Normalize internal parse filenames to match pymupdf4llm path rewriting."""
    candidate_name = (
        os.path.basename(filename) if isinstance(filename, str) and filename else ""
    )
    cleaned_name = (
        path_handle(candidate_name, mode="clean_single") if candidate_name else ""
    )
    name_root, name_ext = os.path.splitext(cleaned_name)
    sanitized_fallback_ext = (
        path_handle(fallback_ext, mode="clean_single") if fallback_ext else ""
    )
    effective_ext = (
        sanitized_fallback_ext
        if prefer_fallback_ext and sanitized_fallback_ext
        else (name_ext or sanitized_fallback_ext)
    )
    effective_root = name_root or "document"
    internal_name = f"{effective_root}{effective_ext}"

    return (
        internal_name.replace("(", "-")
        .replace(")", "-")
        .replace("[", "-")
        .replace("]", "-")
        .replace(" ", "_")
        .replace(chr(0x2010), "-")
        .replace(chr(0x2011), "-")
        .replace(chr(0x2012), "-")
        .replace(chr(0x2013), "-")
        .replace(chr(0x2014), "-")
        .replace(chr(0x2015), "-")
        .replace(chr(0x2212), "-")
    )


def prepare_internal_parse_input(
    temp_file_path: str,
    filename: str | None,
    fallback_ext: str = "",
    prefer_fallback_ext: bool = False,
) -> PreparedInternalParseInput:
    """Normalize the internal filename and move the temp file into that path."""
    internal_filename = normalize_internal_parse_name(
        filename,
        fallback_ext=fallback_ext,
        prefer_fallback_ext=prefer_fallback_ext,
    )
    parse_temp_dir = os.path.dirname(temp_file_path)
    prepared_file_path = os.path.join(parse_temp_dir, internal_filename)
    os.replace(temp_file_path, prepared_file_path)
    return PreparedInternalParseInput(
        internal_filename=internal_filename,
        file_path=prepared_file_path,
    )
