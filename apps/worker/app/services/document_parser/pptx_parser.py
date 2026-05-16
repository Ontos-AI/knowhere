# pyright: reportArgumentType=false, reportCallIssue=false
import io
import os
import re
import time

import jwt
import requests
from app.services.document_parser.path_helpers import find_images
from app.services.document_parser.legacy_converter import (
    _convert_with_libreoffice,
)
from app.services.document_parser.md_parser import parse_md
from app.services.document_parser.mineru_pdf_service import (
    get_existing_mineru_source_s3_key,
)
from app.services.document_parser.parser_log_utils import truncate_log_value
from app.services.document_parser.pdf_parser import parse_pdfs
from app.services.document_parser.pptx_pdf_rendering import (
    render_pdf_to_image_pdf as _render_pdf_to_image_pdf,
)
from loguru import logger
from markitdown import MarkItDown
from pptx2md import ConversionConfig, convert

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    FileSystemException,
)
from shared.core.logging import LogEvent
from shared.services.storage.job_file_storage import JobFileStorage
from shared.utils.file_loading import load_file_bytes
from shared.utils.file_utils import path_handle

# ==================== LibreOffice conversion ====================


def pptx_to_pdf_libreoffice(pptx_path, outdir="."):
    """use LibreOffice to convert PPTX to PDF (local engine, formula rendering may be problematic)"""
    from shared.core.constants import ProcessingConstants

    filter_opts = (
        f"Quality={ProcessingConstants.IMG_QUALITY};"
        "ReduceImageResolution=false;"
        "UseTaggedPDF=true;"
        "ExportNotes=true"
    )
    return _convert_with_libreoffice(
        source_path=pptx_path,
        outdir=outdir,
        convert_to_arg=f"pdf:impress_pdf_Export:{filter_opts}",
        expected_output_ext="pdf",
        operation="convert_pptx_to_pdf",
    )


# ==================== iLoveAPI conversion ====================


def _get_iloveapi_token_lease():
    """acquire iLoveAPI token lease from the quotas pool and generate a JWT token"""
    from shared.utils.iloveapi_quota_manager import get_iloveapi_quota_manager

    quota_manager = get_iloveapi_quota_manager()

    lease = quota_manager.acquire_request("pptx_to_pdf")

    # parse the public_key:secret_key from api_key
    parts = lease.api_key.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid ILOVEAPI key format for token {lease.token_id}")
    public_key, secret_key = parts

    now = int(time.time())
    payload = {
        "iss": "user",
        "jti": public_key,
        "iat": now,
        "nbf": now,
        "exp": now + 7200,  # 2 hours
    }
    token = jwt.encode(payload, secret_key, algorithm="HS256")
    return token, lease


def pptx_to_pdf_api(pptx_path, outdir="."):
    """
    use iloveapi to convert pptx to pdf (file-path based, used by debug_parse.py)
    API docs: https://www.iloveapi.com/docs/api-reference
    """
    with open(pptx_path, "rb") as f:
        pptx_bytes = f.read()
    filename = os.path.basename(pptx_path)
    pdf_bytes = _pptx_bytes_to_pdf_bytes(pptx_bytes, filename)

    base = os.path.splitext(filename)[0]
    pdf_name = base + ".pdf"
    pdf_path = os.path.join(outdir, pdf_name)
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)
    logger.bind(
        event=LogEvent.ILOVEAPI_REQUEST_COMPLETE.value,
        service="iloveapi",
        filename=pdf_name,
        output_size_kb=round(len(pdf_bytes) / 1024, 1),
    ).info(f"[iLoveAPI] PDF saved: {pdf_path}")
    return pdf_path, pdf_name


def _pptx_bytes_to_pdf_bytes(pptx_bytes: bytes, filename: str) -> bytes:
    """
    Convert PPTX bytes → PDF bytes via iLoveAPI. Pure in-memory, no disk I/O.

    Acquires an in-flight slot before starting. Slots are released only if
    the Redis-backed reservation actually succeeded.
    """
    from shared.utils.iloveapi_quota_manager import get_iloveapi_quota_manager

    quota_manager = get_iloveapi_quota_manager()

    # Gate on in-flight concurrency before consuming any token quota
    inflight_acquired = quota_manager.acquire_inflight()
    if inflight_acquired is False:
        logger.bind(
            event=LogEvent.ILOVEAPI_CONCURRENCY_EXCEEDED.value,
            service="iloveapi",
            filename=filename,
            current_inflight=quota_manager.get_inflight_count(),
            max_concurrent=quota_manager.max_concurrent,
        ).warning(f"[iLoveAPI] Concurrency limit reached, failing open for: {filename}")
        raise _ILoveApiConcurrencyExceeded(
            f"iLoveAPI in-flight limit ({quota_manager.max_concurrent}) reached"
        )

    start_time = time.monotonic()
    current_step = "start_task"
    upstream_server = None
    upstream_task_id = None
    server_url = None
    server_filename = None

    try:
        token, lease = _get_iloveapi_token_lease()
        headers = {"Authorization": f"Bearer {token}"}
        base_url = settings.ILOVEAPI_BASE_URL
        timeout = settings.ILOVEAPI_TIMEOUT

        try:
            # Step 1: Start task
            logger.bind(
                event=LogEvent.ILOVEAPI_REQUEST_START.value,
                service="iloveapi",
                step="start_task",
                token_id=lease.token_id,
                filename=filename,
                file_size_kb=round(len(pptx_bytes) / 1024, 1),
            ).info(f"[iLoveAPI] Starting officepdf task for: {filename}")
            res = requests.get(
                f"{base_url}/start/officepdf", headers=headers, timeout=timeout
            )
            if res.status_code == 429:
                retry_after = int(res.headers.get("Retry-After", 60))
                quota_manager.mark_rate_limited(lease.token_id, retry_after)
                logger.bind(
                    event=LogEvent.ILOVEAPI_RATE_LIMITED.value,
                    service="iloveapi",
                    token_id=lease.token_id,
                    retry_after=retry_after,
                    status_code=429,
                    step="start_task",
                ).warning("[iLoveAPI] Rate limited on start_task")
            res.raise_for_status()
            start_data = res.json()
            upstream_server = start_data["server"]
            upstream_task_id = start_data["task"]
            server_url = f"https://{upstream_server}/v1"

            # Step 2: Upload from memory (BytesIO, no disk write)
            current_step = "upload"
            upload_res = requests.post(
                f"{server_url}/upload",
                headers=headers,
                data={"task": upstream_task_id},
                files={"file": (filename, io.BytesIO(pptx_bytes))},
                timeout=timeout,
            )
            if upload_res.status_code == 429:
                retry_after = int(upload_res.headers.get("Retry-After", 60))
                quota_manager.mark_rate_limited(lease.token_id, retry_after)
                logger.bind(
                    event=LogEvent.ILOVEAPI_RATE_LIMITED.value,
                    service="iloveapi",
                    token_id=lease.token_id,
                    retry_after=retry_after,
                    status_code=429,
                    step="upload",
                ).warning("[iLoveAPI] Rate limited on upload")
            upload_res.raise_for_status()
            server_filename = upload_res.json()["server_filename"]

            # Step 3: Process
            current_step = "process"
            process_res = requests.post(
                f"{server_url}/process",
                headers=headers,
                json={
                    "task": upstream_task_id,
                    "tool": "officepdf",
                    "files": [
                        {"server_filename": server_filename, "filename": filename}
                    ],
                },
                timeout=timeout,
            )
            if process_res.status_code == 429:
                retry_after = int(process_res.headers.get("Retry-After", 60))
                quota_manager.mark_rate_limited(lease.token_id, retry_after)
                logger.bind(
                    event=LogEvent.ILOVEAPI_RATE_LIMITED.value,
                    service="iloveapi",
                    token_id=lease.token_id,
                    retry_after=retry_after,
                    status_code=429,
                    step="process",
                ).warning("[iLoveAPI] Rate limited on process")
            process_res.raise_for_status()

            # Step 4: Download PDF to memory
            current_step = "download"
            download_res = requests.get(
                f"{server_url}/download/{upstream_task_id}",
                headers=headers,
                timeout=timeout,
            )
            if download_res.status_code == 429:
                retry_after = int(download_res.headers.get("Retry-After", 60))
                quota_manager.mark_rate_limited(lease.token_id, retry_after)
                logger.bind(
                    event=LogEvent.ILOVEAPI_RATE_LIMITED.value,
                    service="iloveapi",
                    token_id=lease.token_id,
                    retry_after=retry_after,
                    status_code=429,
                    step="download",
                ).warning("[iLoveAPI] Rate limited on download")
            download_res.raise_for_status()

            duration_s = round(time.monotonic() - start_time, 2)
            logger.bind(
                event=LogEvent.ILOVEAPI_REQUEST_COMPLETE.value,
                service="iloveapi",
                token_id=lease.token_id,
                filename=filename,
                duration_s=duration_s,
                input_size_kb=round(len(pptx_bytes) / 1024, 1),
                output_size_kb=round(len(download_res.content) / 1024, 1),
            ).info(f"[iLoveAPI] Conversion complete in {duration_s}s")

            return download_res.content

        except requests.exceptions.HTTPError as e:
            duration_s = round(time.monotonic() - start_time, 2)
            status_code = e.response.status_code if e.response is not None else None
            response_body = None
            response_content_type = None
            response_headers = None
            response_url = None

            if e.response is not None:
                response_body = truncate_log_value(e.response.text)
                response_content_type = e.response.headers.get("Content-Type")
                response_headers = truncate_log_value(dict(e.response.headers))
                response_url = str(e.response.url)

            logger.bind(
                event=LogEvent.ILOVEAPI_REQUEST_FAIL.value,
                service="iloveapi",
                token_id=lease.token_id,
                filename=filename,
                duration_s=duration_s,
                error_type="http_error",
                status_code=status_code,
                step=current_step,
                upstream_server=upstream_server,
                upstream_server_url=server_url,
                upstream_task_id=upstream_task_id,
                upstream_server_filename=server_filename,
                response_url=response_url,
                response_content_type=response_content_type,
                response_headers=response_headers,
                response_body=response_body,
            ).warning(f"[iLoveAPI] HTTP error {status_code} for: {filename}")
            if e.response is not None and e.response.status_code in (401, 403, 429):
                retry_after = int(e.response.headers.get("Retry-After", 60))
                quota_manager.mark_rate_limited(lease.token_id, retry_after)
            raise

    finally:
        if inflight_acquired is True:
            quota_manager.release_inflight()


class _ILoveApiConcurrencyExceeded(Exception):
    """Internal signal: in-flight concurrency limit reached, caller should fallback."""

    pass


def _build_rendered_pdf_s3_key(job_id: str | None) -> str | None:
    """Store rendered parser artifacts under a stable transform/ prefix."""
    if settings.ENVIRONMENT == "development" or not job_id:
        return None
    return f"transform/{job_id}.rendered.pdf"


def _parse_cached_rendered_pdf(
    rendered_pdf_s3_key: str | None,
    filename: str,
    output_dir: str,
    base_llm_paras,
    relative_root,
):
    """Parse a previously rendered PPTX PDF from S3 without re-reading the source deck."""
    if rendered_pdf_s3_key is None:
        return None

    cached_rendered_pdf_s3_key = get_existing_mineru_source_s3_key(rendered_pdf_s3_key)
    if cached_rendered_pdf_s3_key is None:
        return None

    logger.info(
        f"[parse_pptx] Reusing rendered PDF for MinerU URL mode: {rendered_pdf_s3_key}"
    )
    cached_rendered_pdf_path = JobFileStorage().download_upload_to_temp(
        cached_rendered_pdf_s3_key,
        suffix=".pdf",
        temp_dir=output_dir,
    )
    try:
        return parse_pdfs(
            cached_rendered_pdf_path,
            filename,
            output_dir,
            base_llm_paras,
            relative_root=relative_root,
            s3_key=cached_rendered_pdf_s3_key,
        )
    finally:
        if os.path.exists(cached_rendered_pdf_path):
            os.remove(cached_rendered_pdf_path)


# ==================== main parsing entrance ====================


def parse_pptx(
    pptx_path,
    filename,
    output_dir,
    base_llm_paras,
    strategy="to_pdf_api",
    relative_root=None,
    baseurl="",
    job_id=None,
):
    """
    PPTX parsing entrance, aligned with parse_pdfs / parse_docx pattern.

    strategy options:
        - "to_md":      directly extract from PPTX XML (pptx2md + MarkItDown)
        - "to_pdf":     use LibreOffice to convert to PDF, then parse via MinerU
        - "to_pdf_api": use iLoveAPI to convert to PDF, then parse via MinerU (recommended)
    """
    rendered_pdf_s3_key = (
        _build_rendered_pdf_s3_key(job_id)
        if strategy in {"to_pdf_api", "to_pdf"}
        else None
    )
    if strategy in {"to_pdf_api", "to_pdf"}:
        cached_result = _parse_cached_rendered_pdf(
            rendered_pdf_s3_key=rendered_pdf_s3_key,
            filename=filename,
            output_dir=output_dir,
            base_llm_paras=base_llm_paras,
            relative_root=relative_root,
        )
        if cached_result is not None:
            return cached_result

    pptx_data = load_file_bytes(pptx_path, file_url=baseurl)
    logger.info(f"[parse_pptx] PPTX loaded: {len(pptx_data) / 1024:.1f} KB")

    if strategy == "to_pdf_api":
        from shared.core.exceptions.domain_exceptions import UnavailableException

        try:
            return _parse_pptx_via_api(
                pptx_data,
                filename,
                output_dir,
                base_llm_paras,
                relative_root,
                rendered_pdf_s3_key=rendered_pdf_s3_key,
            )
        except ValueError as e:
            if "iLoveAPI keys configured" in str(e) or "ILOVEAPI" in str(e):
                logger.bind(
                    event=LogEvent.ILOVEAPI_FALLBACK.value,
                    service="iloveapi",
                    reason="config_missing",
                    filename=filename,
                    fallback_strategy="to_pdf",
                ).warning(
                    f"[parse_pptx] iLoveAPI config missing: {e}. Falling back to 'to_pdf' (LibreOffice)."
                )
                strategy = "to_pdf"
            else:
                raise
        except _ILoveApiConcurrencyExceeded as e:
            logger.bind(
                event=LogEvent.ILOVEAPI_FALLBACK.value,
                service="iloveapi",
                reason="concurrency_exceeded",
                filename=filename,
                fallback_strategy="to_pdf",
            ).warning(
                f"[parse_pptx] iLoveAPI concurrency limit reached: {e}. Falling back to 'to_pdf' (LibreOffice)."
            )
            strategy = "to_pdf"
        except UnavailableException as e:
            logger.bind(
                event=LogEvent.ILOVEAPI_FALLBACK.value,
                service="iloveapi",
                reason="pool_exhausted",
                filename=filename,
                fallback_strategy="to_pdf",
                retry_after=e.retry_after,
            ).warning(
                f"[parse_pptx] iLoveAPI token pool exhausted: {e.internal_message}. Falling back to 'to_pdf' (LibreOffice)."
            )
            strategy = "to_pdf"
        except requests.exceptions.RequestException as e:
            error_type = type(e).__name__
            logger.bind(
                event=LogEvent.ILOVEAPI_FALLBACK.value,
                service="iloveapi",
                reason="request_failed",
                filename=filename,
                fallback_strategy="to_pdf",
                error_type=error_type,
            ).warning(
                f"[parse_pptx] iLoveAPI request failed ({error_type}): {e}. Falling back to 'to_pdf' (LibreOffice)."
            )
            strategy = "to_pdf"

    if strategy == "to_pdf":
        return _parse_pptx_via_libreoffice(
            pptx_data,
            filename,
            output_dir,
            base_llm_paras,
            relative_root,
            rendered_pdf_s3_key=rendered_pdf_s3_key,
        )

    elif strategy == "to_md":
        return _parse_pptx_to_md(
            pptx_data, filename, output_dir, base_llm_paras, relative_root
        )

    else:
        raise ValueError(f"Unknown pptx strategy: {strategy}")


def _parse_pptx_via_api(
    pptx_data,
    filename,
    output_dir,
    base_llm_paras,
    relative_root,
    rendered_pdf_s3_key=None,
):
    """
    PPTX bytes → iLoveAPI PDF bytes → image-only PDF bytes → MinerU.

    In production, if a job id is available, upload the rendered image-only PDF
    to a deterministic transform key and let MinerU fetch that via URL mode.
    This preserves the OCR-friendly rendered-PDF behavior while avoiding a long
    worker-to-MinerU upload.
    """
    # Step 1: PPTX → PDF (in memory)
    pdf_bytes = _pptx_bytes_to_pdf_bytes(pptx_data, filename)

    # Step 2: PDF → image-only PDF (in memory)
    img_pdf_bytes = _render_pdf_to_image_pdf(pdf_bytes)

    # Step 3: Write to output_dir for MinerU upload, then clean up
    tmp_path = os.path.join(output_dir, "_pptx_tmp.pdf")
    with open(tmp_path, "wb") as f:
        f.write(img_pdf_bytes)

    try:
        parsed_df = parse_pdfs(
            tmp_path,
            filename=filename,
            output_dir=output_dir,
            base_llm_paras=base_llm_paras,
            relative_root=relative_root,
            s3_key=rendered_pdf_s3_key,
        )
        return parsed_df
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _parse_pptx_via_libreoffice(
    pptx_data,
    filename,
    output_dir,
    base_llm_paras,
    relative_root,
    rendered_pdf_s3_key=None,
):
    """
    LibreOffice requires file paths (subprocess), so temp dir is unavoidable here.
    PPTX → temp file → LibreOffice → PDF → image-only PDF bytes → MinerU.
    """
    import shutil
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="pptx_lo_")
    try:
        # Write PPTX to temp (LibreOffice needs file path)
        local_pptx = os.path.join(tmp_dir, filename)
        local_pptx = path_handle(local_pptx, mode="sanitize")
        with open(local_pptx, "wb") as f:
            f.write(pptx_data)

        pdf_path, _ = pptx_to_pdf_libreoffice(local_pptx, tmp_dir)

        # Read PDF into memory, then same image-only PDF flow
        try:
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
        except OSError as exc:
            raise FileSystemException(
                internal_message=f"Failed to read converted PPTX PDF at '{pdf_path}'",
                operation="read",
                original_exception=exc,
            ) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    img_pdf_bytes = _render_pdf_to_image_pdf(pdf_bytes)

    tmp_path = os.path.join(output_dir, "_pptx_tmp.pdf")
    with open(tmp_path, "wb") as f:
        f.write(img_pdf_bytes)

    try:
        parsed_df = parse_pdfs(
            tmp_path,
            filename=filename,
            output_dir=output_dir,
            base_llm_paras=base_llm_paras,
            relative_root=relative_root,
            s3_key=rendered_pdf_s3_key,
        )
        return parsed_df
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _parse_pptx_to_md(pptx_data, filename, output_dir, base_llm_paras, relative_root):
    """Extract content from PPTX XML via pptx2md + MarkItDown → parse_md."""
    # pptx2md and MarkItDown require file paths
    local_pptx = os.path.join(output_dir, "_pptx_tmp.pptx")
    with open(local_pptx, "wb") as f:
        f.write(pptx_data)

    try:
        img_dir = os.path.join(output_dir, "images")
        os.makedirs(img_dir, exist_ok=True)
        temp_md_path = os.path.join(output_dir, "output.md")

        convert(
            ConversionConfig(
                pptx_path=local_pptx, output_path=temp_md_path, image_dir=img_dir
            )
        )

        md = MarkItDown(enable_plugins=False)
        result = md.convert(local_pptx)

        pattern = r"^!\[.*?\]\(.*?\.(?:png|jpe?g)\)$"
        md_imgs = find_images(output_dir)
        lines = result.text_content.splitlines()

        ppt_md_lines = []
        image_index = 0
        for line in lines:
            if image_index < len(md_imgs):
                if re.match(pattern, line.strip(), re.IGNORECASE):
                    line = f"![image{image_index + 1}]({md_imgs[image_index]})"
                    image_index += 1
            ppt_md_lines.append(line)

        while image_index < len(md_imgs):
            ppt_md_lines.append(f"![image{image_index + 1}]({md_imgs[image_index]})")
            image_index += 1

        parsed_df = parse_md(
            output_dir,
            source_type="pptx",
            md_lines=ppt_md_lines,
            base_llm_paras=base_llm_paras,
            relative_root=relative_root,
        )
        return parsed_df
    finally:
        if os.path.exists(local_pptx):
            os.remove(local_pptx)
