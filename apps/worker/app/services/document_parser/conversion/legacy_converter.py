import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.services.document_parser.support.parser_log_utils import truncate_log_value

from shared.core.exceptions.domain_exceptions import LibreOfficeServiceException


def resolve_libreoffice_binary() -> str:
    """Resolve a LibreOffice/soffice binary from the system installation."""
    candidate_paths: list[str] = []

    for binary in ("libreoffice", "soffice"):
        resolved = shutil.which(binary)
        if resolved:
            candidate_paths.append(resolved)

    candidate_paths.extend(
        [
            "/usr/bin/libreoffice",
            "/usr/bin/soffice",
            "/opt/homebrew/bin/soffice",
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        ]
    )

    seen: set[str] = set()
    for candidate in candidate_paths:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate and os.path.isfile(candidate):
            return candidate

    raise LibreOfficeServiceException(
        internal_message=(
            "LibreOffice binary not found. "
            f"Checked system PATH and common binaries: {candidate_paths}"
        ),
        operation="resolve_binary",
    )


def _convert_with_libreoffice(
    source_path: str,
    outdir: str,
    convert_to_arg: str,
    expected_output_ext: str,
    operation: str,
) -> tuple[str, str]:
    """Convert a legacy Office document to an OOXML format via LibreOffice."""
    soffice_path = resolve_libreoffice_binary()

    os.makedirs(outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(source_path))[0]
    output_name = f"{base}.{expected_output_ext}"
    output_path = os.path.join(outdir, output_name)

    # Isolate the LibreOffice user profile to avoid concurrent worker collisions.
    with tempfile.TemporaryDirectory(prefix="libreoffice-profile-") as profile_dir:
        profile_uri = Path(profile_dir).resolve().as_uri()
        conversion_result = subprocess.run(
            [
                soffice_path,
                f"-env:UserInstallation={profile_uri}",
                "--headless",
                "--convert-to",
                convert_to_arg,
                source_path,
                "--outdir",
                outdir,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    if conversion_result.returncode != 0:
        raise LibreOfficeServiceException(
            internal_message=(
                f"LibreOffice conversion failed with exit code {conversion_result.returncode}: "
                f"input={source_path}, output_dir={outdir}, expected_output={output_path}, "
                f"stdout={truncate_log_value(conversion_result.stdout)}, "
                f"stderr={truncate_log_value(conversion_result.stderr)}"
            ),
            operation=operation,
            exit_code=conversion_result.returncode,
        )

    if not os.path.exists(output_path):
        raise LibreOfficeServiceException(
            internal_message=(
                "LibreOffice did not produce the expected output: "
                f"input={source_path}, output_dir={outdir}, expected_output={output_path}, "
                f"stdout={truncate_log_value(conversion_result.stdout)}, "
                f"stderr={truncate_log_value(conversion_result.stderr)}"
            ),
            operation=f"emit_{expected_output_ext}_output",
        )

    return output_path, output_name


def doc_to_docx(doc_path: str, outdir: str = ".") -> tuple[str, str]:
    """Convert legacy `.doc` to `.docx` via LibreOffice."""
    return _convert_with_libreoffice(
        source_path=doc_path,
        outdir=outdir,
        convert_to_arg="docx",
        expected_output_ext="docx",
        operation="convert_doc_to_docx",
    )


def xls_to_xlsx(xls_path: str, outdir: str = ".") -> tuple[str, str]:
    """Convert legacy `.xls` to `.xlsx` via LibreOffice."""
    return _convert_with_libreoffice(
        source_path=xls_path,
        outdir=outdir,
        convert_to_arg="xlsx",
        expected_output_ext="xlsx",
        operation="convert_xls_to_xlsx",
    )
