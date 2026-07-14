"""Render DOCX through a clearly identified LibreOffice normalized PDF."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from app.services.document_parser.conversion.legacy_converter import (
    resolve_libreoffice_binary,
)
from app.services.document_parser.support.parser_log_utils import truncate_log_value
from shared.core.exceptions.domain_exceptions import LibreOfficeServiceException


def probe_libreoffice_version(binary: str | None = None) -> str | None:
    """Return a concise converter version when the binary can report one."""
    try:
        executable = binary or resolve_libreoffice_binary()
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError, LibreOfficeServiceException):
        return None
    if result.returncode != 0:
        return None
    version = (result.stdout or result.stderr).strip()
    return version or None


def render_docx_to_normalized_pdf(
    *,
    docx_path: Path,
    output_dir: Path,
) -> Path:
    """Convert DOCX to `source.pdf` without implying native DOCX page stability."""
    source = docx_path.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".docx":
        raise ValueError("DOCX path must be an existing local .docx file.")
    destination_dir = output_dir.expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    soffice_path = resolve_libreoffice_binary()

    with tempfile.TemporaryDirectory(prefix="libreoffice-profile-") as profile_dir:
        profile_uri = Path(profile_dir).resolve().as_uri()
        try:
            result = subprocess.run(
                [
                    soffice_path,
                    f"-env:UserInstallation={profile_uri}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    str(source),
                    "--outdir",
                    str(destination_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
                shell=False,
                timeout=300,
            )
        except subprocess.TimeoutExpired as error:
            raise LibreOfficeServiceException(
                internal_message="LibreOffice DOCX-to-PDF conversion timed out.",
                operation="convert_docx_to_pdf",
            ) from error
        except OSError as error:
            raise LibreOfficeServiceException(
                internal_message="LibreOffice DOCX-to-PDF process could not start.",
                operation="convert_docx_to_pdf",
            ) from error

    produced_pdf = destination_dir / f"{source.stem}.pdf"
    if result.returncode != 0:
        raise LibreOfficeServiceException(
            internal_message=(
                "LibreOffice DOCX-to-PDF conversion failed: "
                f"stdout={truncate_log_value(result.stdout)}, "
                f"stderr={truncate_log_value(result.stderr)}"
            ),
            operation="convert_docx_to_pdf",
            exit_code=result.returncode,
        )
    if not produced_pdf.is_file():
        raise LibreOfficeServiceException(
            internal_message=(
                "LibreOffice did not produce the normalized PDF: "
                f"stdout={truncate_log_value(result.stdout)}, "
                f"stderr={truncate_log_value(result.stderr)}"
            ),
            operation="emit_docx_pdf_output",
        )

    normalized_pdf = destination_dir / "source.pdf"
    if produced_pdf != normalized_pdf:
        if normalized_pdf.exists():
            normalized_pdf.unlink()
        shutil.move(str(produced_pdf), normalized_pdf)
    return normalized_pdf

