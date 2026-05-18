"""Download-and-extract helpers for remote ZIP artifacts."""

import os
import zipfile
from pathlib import Path
from collections.abc import Mapping

import requests


def download_and_extract_zip(
    url: str,
    dest_dir: str | os.PathLike[str],
    *,
    filename: str = "parsed.zip",
    headers: Mapping[str, str] | None = None,
    timeout: int | None = None,
    chunk_size: int | None = None,
    keep_exts: tuple[str, ...] = (".md", ".json"),
    exclude_patterns: tuple[str, ...] = (),
    clean_empty_dirs: bool = True,
) -> None:
    """Download a ZIP file, extract it, and keep only the requested artifacts."""
    import fnmatch

    from shared.core.constants import APIConstants, ProcessingConstants

    if timeout is None:
        timeout = APIConstants.S3_FILE_DOWNLOAD_TIMEOUT
    if chunk_size is None:
        chunk_size = ProcessingConstants.IMG_CHUNK_SIZE

    destination = Path(dest_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    zip_path = destination / filename

    with requests.get(
        url,
        headers=headers or {},
        timeout=timeout,
        stream=True,
        allow_redirects=True,
    ) as response:
        response.raise_for_status()
        with open(zip_path, "wb") as zip_file:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    zip_file.write(chunk)

    with zipfile.ZipFile(zip_path, "r") as extracted_zip:
        extracted_zip.extractall(destination)

    for extracted_path in destination.rglob("*"):
        if not extracted_path.is_file():
            continue

        should_exclude = False
        for pattern in exclude_patterns:
            if pattern in extracted_path.name or fnmatch.fnmatch(extracted_path.name, pattern):
                should_exclude = True
                break

        if should_exclude:
            extracted_path.unlink()
        elif extracted_path.suffix.lower() not in keep_exts:
            extracted_path.unlink()

    if clean_empty_dirs:
        for directory in sorted(
            [path for path in destination.rglob("*") if path.is_dir()],
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                next(directory.iterdir())
            except StopIteration:
                directory.rmdir()

    zip_path.unlink(missing_ok=True)
