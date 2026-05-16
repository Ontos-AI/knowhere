"""Sync file-loading helpers for worker parsing paths."""

from pathlib import Path
from urllib.parse import ParseResult

import httpx


def is_remote(path: object) -> bool:
    """Return True if `path` is an HTTP(S) URL."""
    if path is None or not isinstance(path, str):
        return False
    return path.startswith("http://") or path.startswith("https://")


def load_file_bytes(
    file_path: str | Path,
    *,
    file_url: str | ParseResult = "",
    timeout: float | None = None,
) -> bytes:
    """Load bytes from local path or remote URL synchronously."""
    if isinstance(file_path, str) and is_remote(file_path):
        url_to_use = file_path
        if not isinstance(file_url, str):
            file_url = file_url.geturl()
        if file_url:
            url_to_use = file_url

        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url_to_use)
            response.raise_for_status()
            return response.content

    return Path(file_path).read_bytes()
