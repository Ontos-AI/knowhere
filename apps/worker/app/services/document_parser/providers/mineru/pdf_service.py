import os
from typing import Optional

import requests
from app.services.document_parser.providers.mineru.client import (
    get_mineru_headers,
    get_mineru_session,
    mineru_logger,
    raise_mineru_unavailable,
)
from app.services.document_parser.providers.mineru.quota_manager import get_mineru_quota_manager
from app.services.document_parser.providers.mineru.task_polling import (
    get_batch_status,
    poll_mineru_task,
)
from app.services.document_parser.support.parser_log_utils import truncate_log_value

from shared.core.config import settings
from shared.core.constants import APIConstants
from shared.core.exceptions.domain_exceptions import (
    MinerUServiceException,
    StorageServiceException,
    UnavailableException,
)
from shared.services.storage.job_file_storage import JobFileStorage
from shared.utils.zip_download import download_and_extract_zip
from app.services.common.file_loading import is_remote

MINERU_UPLOAD_TIMEOUT = (
    settings.MINERU_UPLOAD_CONNECT_TIMEOUT,
    settings.MINERU_UPLOAD_READ_TIMEOUT,
)


def _should_use_mineru_s3_url_mode(s3_key: Optional[str]) -> bool:
    return not settings.MINERU_UPLOAD_MODE_ENABLED and s3_key is not None


def _log_mineru_url_mode_storage_fallback(
    operation: str,
    s3_key: str,
    local_file_path: Optional[str],
    exc: Exception,
) -> None:
    mineru_logger(
        "url_mode_storage_fallback",
        operation=operation,
        source_s3_key=s3_key,
        local_file_path=local_file_path,
        error_type=type(exc).__name__,
        error_message=truncate_log_value(exc),
    ).warning(
        "MinerU URL-mode storage preparation failed. Falling back to direct upload."
    )


def _log_mineru_url_mode_ingestion_fallback(
    operation: str,
    s3_key: str,
    pdf_url: str,
    exc: Exception,
) -> None:
    mineru_logger(
        "url_mode_ingestion_fallback",
        operation=operation,
        source_s3_key=s3_key,
        source_kind="remote_url" if is_remote(pdf_url) else "local_file",
        source_path=None if is_remote(pdf_url) else pdf_url,
        error_type=type(exc).__name__,
        error_message=truncate_log_value(exc),
    ).warning("MinerU URL-mode ingestion setup failed. Falling back to direct upload.")


def _inspect_mineru_source_s3_key(s3_key: Optional[str]) -> tuple[Optional[str], bool]:
    """Inspect whether URL mode can reuse or prepare the requested S3 source key."""
    if not _should_use_mineru_s3_url_mode(s3_key):
        return None, False

    assert s3_key is not None
    try:
        existing_file = JobFileStorage().verify_upload_exists(s3_key)
    except Exception as exc:
        _log_mineru_url_mode_storage_fallback(
            operation="verify_source_object",
            s3_key=s3_key,
            local_file_path=None,
            exc=exc,
        )
        return None, False

    if existing_file.get("exists"):
        mineru_logger(
            "url_mode_source_reused",
            source_s3_key=s3_key,
        ).info("Reusing existing S3 source for MinerU URL mode")
        return s3_key, True

    return None, True


def get_existing_mineru_source_s3_key(s3_key: Optional[str]) -> Optional[str]:
    """Return an existing S3 source key for URL mode, or None if it is unavailable."""
    existing_s3_key, _ = _inspect_mineru_source_s3_key(s3_key)
    return existing_s3_key


def resolve_mineru_source_s3_key(
    s3_key: Optional[str],
    local_file_path: Optional[str] = None,
) -> Optional[str]:
    """Resolve an S3 source key for URL mode, uploading a local file if needed."""
    existing_s3_key, can_prepare_url_mode = _inspect_mineru_source_s3_key(s3_key)
    if existing_s3_key is not None:
        return existing_s3_key

    if not can_prepare_url_mode:
        return None

    if local_file_path is None or is_remote(local_file_path):
        return None

    assert s3_key is not None
    try:
        JobFileStorage().upload_source_file(local_file_path, s3_key)
    except Exception as exc:
        _log_mineru_url_mode_storage_fallback(
            operation="upload_source_object",
            s3_key=s3_key,
            local_file_path=local_file_path,
            exc=exc,
        )
        return None

    mineru_logger(
        "url_mode_source_uploaded",
        source_s3_key=s3_key,
        local_file_path=local_file_path,
    ).info("Uploaded local PDF to S3 for MinerU URL mode")
    return s3_key



def _request_upload_target(pdf_url: str, filename: str) -> tuple[str, str, str]:
    base_url = settings.MINERU_URL
    quota_manager = get_mineru_quota_manager()
    upload_logger = mineru_logger(
        "upload_url",
        operation="upload_url",
        filename=filename,
        source_kind="remote_url" if is_remote(pdf_url) else "local_file",
    )
    url = f"{base_url}/file-urls/batch"
    payload = {
        "files": [
            {
                "name": filename,
                "is_ocr": True,
            }
        ],
        "enable_formula": True,
        "enable_table": True,
        "language": "auto",
        "model_version": "vlm",
    }

    upload_logger.info("Requesting MinerU upload URL")
    lease = quota_manager.acquire_request(operation="upload_url")
    upload_logger.bind(token_id=lease.token_id).info(
        "Acquired MinerU token for upload URL"
    )
    response = get_mineru_session().post(
        url,
        headers=get_mineru_headers(lease.api_key),
        json=payload,
        timeout=settings.MINERU_API_TIMEOUT,
    )
    if response.status_code == 429:
        raise_mineru_unavailable(lease.token_id, response, operation="upload_url")
    if response.status_code != 200:
        upload_logger.bind(
            token_id=lease.token_id,
            status_code=response.status_code,
        ).error("Failed to get MinerU upload URL")
        raise MinerUServiceException(
            internal_message=f"Failed to get upload URL: {response.text}",
            status_code=response.status_code,
        )

    result = response.json()
    if result.get("code") != 0:
        response_message = str(result.get("msg", "Unknown error"))
        if "rate limit" in response_message.lower():
            quota_manager.mark_rate_limited(
                lease.token_id,
                settings.MINERU_TOKEN_COOLDOWN_SECONDS,
            )
            upload_logger.bind(
                token_id=lease.token_id,
                retry_after=settings.MINERU_TOKEN_COOLDOWN_SECONDS,
                error_message=response_message,
            ).warning("MinerU upload URL request hit rate limit")
            raise UnavailableException(
                internal_message=f"MinerU rate limited during upload_url: {response_message}",
                retry_after=settings.MINERU_TOKEN_COOLDOWN_SECONDS,
                limit=lease.rpm_limit,
                period="minute",
                user_message="Document processing is busy right now. Please retry shortly.",
            )
        upload_logger.bind(
            token_id=lease.token_id,
            error_message=response_message,
        ).error("MinerU upload URL request returned API error")
        raise MinerUServiceException(
            internal_message=f"MinerU API error: {response_message}"
        )

    batch_id = result["data"]["batch_id"]
    upload_url = result["data"]["file_urls"][0]
    upload_logger.bind(token_id=lease.token_id, batch_id=batch_id).info(
        "Received MinerU upload URL"
    )
    return batch_id, upload_url, lease.token_id


def _upload_file_to_mineru(
    pdf_url: str, filename: str, upload_url: str, token_id: str
) -> None:
    upload_logger = mineru_logger(
        "file_upload",
        operation="file_upload",
        filename=filename,
        token_id=token_id,
        source_kind="remote_url" if is_remote(pdf_url) else "local_file",
    )

    if is_remote(pdf_url):
        import tempfile

        upload_logger.info("Downloading remote source file before MinerU upload")
        try:
            download_response = get_mineru_session().get(
                pdf_url,
                stream=True,
                timeout=APIConstants.S3_FILE_DOWNLOAD_TIMEOUT,
            )
            download_response.raise_for_status()

            with tempfile.NamedTemporaryFile(
                delete=False, suffix=os.path.splitext(filename)[1]
            ) as temp_file:
                for chunk in download_response.iter_content(chunk_size=8192):
                    temp_file.write(chunk)
                temp_path = temp_file.name

            upload_logger.bind(temp_file_path=temp_path).info(
                "Uploading staged file to MinerU"
            )
            with open(temp_path, "rb") as file_obj:
                upload_response = get_mineru_session().put(
                    upload_url,
                    data=file_obj,
                    timeout=MINERU_UPLOAD_TIMEOUT,
                )

            os.unlink(temp_path)
        except requests.RequestException as exc:
            upload_logger.bind(error_message=str(exc)).error(
                "Failed to stage remote source file for MinerU"
            )
            raise StorageServiceException(
                internal_message=f"Failed to download remote file: {exc}"
            )
    else:
        upload_logger.bind(local_path=pdf_url).info("Uploading local file to MinerU")
        try:
            with open(pdf_url, "rb") as file_obj:
                try:
                    upload_response = get_mineru_session().put(
                        upload_url,
                        data=file_obj,
                        timeout=MINERU_UPLOAD_TIMEOUT,
                    )
                except requests.RequestException as exc:
                    upload_logger.bind(error_message=str(exc)).error(
                        "Failed to upload local file to MinerU"
                    )
                    raise MinerUServiceException(
                        internal_message=f"Failed to upload file to MinerU: {exc}",
                        original_exception=exc,
                    ) from exc
        except OSError as exc:
            upload_logger.bind(error_message=str(exc)).error(
                "Failed to read local file for MinerU upload"
            )
            raise StorageServiceException(
                internal_message=f"Failed to read local file: {exc}",
                original_exception=exc,
            ) from exc

    if upload_response.status_code != 200:
        upload_logger.bind(status_code=upload_response.status_code).error(
            "MinerU file upload failed"
        )
        raise MinerUServiceException(
            internal_message=f"Failed to upload file to MinerU: {upload_response.text}",
            status_code=upload_response.status_code,
        )

    upload_logger.info("MinerU file upload completed, switching to polling")


def _submit_url_task(presigned_url: str, filename: str) -> tuple[str, str]:
    """Submit a URL-based extraction task to MinerU.

    Uses the /extract/task/batch endpoint so MinerU fetches the file
    directly from our S3 via presigned URL, skipping the OSS upload hop.

    Returns (batch_id, token_id).
    """
    base_url = settings.MINERU_URL
    quota_manager = get_mineru_quota_manager()
    submit_logger = mineru_logger(
        "submit_url_task",
        operation="submit_url_task",
        filename=filename,
    )

    url = f"{base_url}/extract/task/batch"
    payload = {
        "files": [{"url": presigned_url}],
        "is_ocr": True,
        "enable_formula": True,
        "enable_table": True,
        "language": "auto",
        "model_version": "vlm",
    }

    submit_logger.info("Submitting URL-based MinerU extraction task")
    lease = quota_manager.acquire_request(operation="submit_url_task")
    submit_logger.bind(token_id=lease.token_id).info(
        "Acquired MinerU token for URL task submission"
    )

    response = get_mineru_session().post(
        url,
        headers=get_mineru_headers(lease.api_key),
        json=payload,
        timeout=settings.MINERU_API_TIMEOUT,
    )

    if response.status_code == 429:
        raise_mineru_unavailable(lease.token_id, response, operation="submit_url_task")

    if response.status_code != 200:
        submit_logger.bind(
            token_id=lease.token_id,
            status_code=response.status_code,
        ).error("MinerU URL task submission failed")
        raise MinerUServiceException(
            internal_message=f"URL task submission failed: {response.text}",
            status_code=response.status_code,
        )

    result = response.json()
    if result.get("code") != 0:
        response_message = str(result.get("msg", "Unknown error"))
        if "rate limit" in response_message.lower():
            quota_manager.mark_rate_limited(
                lease.token_id,
                settings.MINERU_TOKEN_COOLDOWN_SECONDS,
            )
            raise UnavailableException(
                internal_message=f"MinerU rate limited during submit_url_task: {response_message}",
                retry_after=settings.MINERU_TOKEN_COOLDOWN_SECONDS,
                limit=lease.rpm_limit,
                period="minute",
                user_message="Document processing is busy right now. Please retry shortly.",
            )
        raise MinerUServiceException(
            internal_message=f"MinerU API error: {response_message}"
        )

    batch_id = result["data"]["batch_id"]
    submit_logger.bind(token_id=lease.token_id, batch_id=batch_id).info(
        "MinerU URL task submitted"
    )
    return batch_id, lease.token_id


def _flatten_extracted_zip(output_dir: str) -> None:
    """Flatten a local MinerU ZIP layout into the shape downstream code expects.

    Local MinerU extracts to ``{stem}/auto/{stem}.md`` plus
    ``{stem}/auto/images/*``. Downstream code expects ``full.md`` and an
    ``images/`` directory at the output dir root. This lifts the contents
    of the single ``{stem}/auto/`` directory to the output root (preserving
    the ``images/`` subdir), removes the ``{stem}/`` wrapper, drops files
    outside the keep set, and renames the single markdown file to
    ``full.md``. Hard-fails on zero or multiple ``.md`` files so we never
    silently pick the wrong one.
    """
    import shutil
    from pathlib import Path

    destination = Path(output_dir).resolve()
    keep_exts = (".md", ".jpg", ".jpeg", ".png", ".gif", ".json")
    exclude_patterns = ("content_list", "middle.json", "model.json")

    auto_dirs = [p for p in destination.glob("*/auto") if p.is_dir()]
    if not auto_dirs:
        raise MinerUServiceException(
            internal_message=(
                "Local MinerU ZIP did not contain a {stem}/auto/ directory; "
                "layout has changed or the response was not a parse result."
            ),
        )
    if len(auto_dirs) > 1:
        relative_paths = ", ".join(str(p.relative_to(destination)) for p in auto_dirs)
        raise MinerUServiceException(
            internal_message=(
                f"Local MinerU ZIP contained {len(auto_dirs)} */auto directories; "
                f"expected exactly one: {relative_paths}"
            ),
        )

    auto_dir = auto_dirs[0]
    for source_path in auto_dir.rglob("*"):
        if source_path.is_dir():
            continue
        relative = source_path.relative_to(auto_dir)
        if any(pattern in source_path.name for pattern in exclude_patterns):
            continue
        if source_path.suffix.lower() not in keep_exts:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            continue
        shutil.move(str(source_path), str(target))

    shutil.rmtree(auto_dir.parent, ignore_errors=True)

    markdown_files = sorted(destination.glob("*.md"))
    if len(markdown_files) == 0:
        raise MinerUServiceException(
            internal_message=(
                "Local MinerU ZIP contained no markdown file under {stem}/auto/."
            ),
        )
    if len(markdown_files) > 1:
        relative_paths = ", ".join(str(p.relative_to(destination)) for p in markdown_files)
        raise MinerUServiceException(
            internal_message=(
                f"Local MinerU ZIP contained {len(markdown_files)} markdown files; "
                f"expected exactly one: {relative_paths}"
            ),
        )

    markdown_files[0].rename(destination / "full.md")


def _get_local_mineru_session() -> requests.Session:
    """Build a session for local MinerU's synchronous /file_parse endpoint.

    Local MinerU is single-concurrency by default
    (``max_concurrent_requests=1``). A ``ReadTimeout`` from queue wait must
    not cascade into urllib3 retries that push the request to the back of
    the same queue, so ``read`` retries are disabled. 429s surface as
    ``UnavailableException`` for upstream retry handling and do not go
    through the cloud quota manager (local mode has no API key).
    """
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    retry_strategy = Retry(
        total=settings.MINERU_UPLOAD_RETRY_TOTAL,
        backoff_factor=settings.MINERU_UPLOAD_RETRY_BACKOFF_FACTOR,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
        read=0,
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=1,
        pool_maxsize=settings.MINERU_POOL_MAXSIZE,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_local_mineru_session: Optional[requests.Session] = None


def _get_local_mineru_session_cached() -> requests.Session:
    global _local_mineru_session
    if _local_mineru_session is None:
        _local_mineru_session = _get_local_mineru_session()
    return _local_mineru_session


def parse_via_local(
    pdf_url: str,
    filename: str,
    output_dir: str,
) -> None:
    """Parse a PDF via a local MinerU instance's synchronous /file_parse.

    Self-hosted deployments that run MinerU on their own network cannot use
    the cloud-only batch APIs (``/file-urls/batch``, ``/extract/task/batch``,
    ``/extract-results/batch``). Local MinerU exposes a single synchronous
    ``/file_parse`` endpoint that accepts the PDF as multipart form data and
    returns a ZIP with a different layout (``{stem}/auto/{stem}.md``).
    """
    base_url = settings.MINERU_URL.rstrip("/")
    endpoint = f"{base_url}/file_parse"
    local_logger = mineru_logger(
        "local_file_parse",
        operation="local_file_parse",
        filename=filename,
        endpoint=endpoint,
        lang_list=settings.MINERU_LOCAL_LANG_LIST,
        backend=settings.MINERU_LOCAL_BACKEND,
    )

    form_fields = {
        "lang_list": settings.MINERU_LOCAL_LANG_LIST,
        "backend": settings.MINERU_LOCAL_BACKEND,
        "return_images": "true",
    }

    if is_remote(pdf_url):
        import tempfile

        local_logger.info("Downloading remote source file before local MinerU parse")
        try:
            download_response = requests.get(
                pdf_url,
                stream=True,
                timeout=APIConstants.S3_FILE_DOWNLOAD_TIMEOUT,
            )
            download_response.raise_for_status()
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=os.path.splitext(filename)[1]
            ) as temp_file:
                for chunk in download_response.iter_content(chunk_size=8192):
                    temp_file.write(chunk)
                temp_path = temp_file.name
        except requests.RequestException as exc:
            local_logger.bind(error_message=str(exc)).error(
                "Failed to stage remote source file for local MinerU"
            )
            raise StorageServiceException(
                internal_message=f"Failed to download remote file: {exc}"
            )
        local_file_path = temp_path
        cleanup_temp = True
    else:
        local_logger.bind(local_path=pdf_url).info(
            "Uploading local file to local MinerU"
        )
        local_file_path = pdf_url
        cleanup_temp = False

    try:
        with open(local_file_path, "rb") as file_obj:
            files = {"file": (filename, file_obj, "application/pdf")}
            local_logger.info("Posting PDF to local MinerU /file_parse")
            try:
                response = _get_local_mineru_session_cached().post(
                    endpoint,
                    data=form_fields,
                    files=files,
                    timeout=settings.MINERU_LOCAL_TIMEOUT,
                )
            except requests.RequestException as exc:
                local_logger.bind(error_type=type(exc).__name__).error(
                    "Local MinerU /file_parse request failed"
                )
                raise MinerUServiceException(
                    internal_message=f"Local MinerU /file_parse failed: {exc}",
                    original_exception=exc,
                ) from exc
    finally:
        if cleanup_temp:
            os.unlink(local_file_path)

    if response.status_code == 429:
        retry_after = 60
        local_logger.bind(
            status_code=response.status_code,
            retry_after=retry_after,
        ).warning("Local MinerU /file_parse rate-limited")
        raise UnavailableException(
            internal_message="Local MinerU rate limited during /file_parse",
            retry_after=retry_after,
            limit=1,
            period="minute",
            user_message="Document processing is busy right now. Please retry shortly.",
        )
    if response.status_code != 200:
        local_logger.bind(status_code=response.status_code).error(
            "Local MinerU /file_parse failed"
        )
        raise MinerUServiceException(
            internal_message=(
                f"Local MinerU /file_parse returned {response.status_code}: "
                f"{response.text[:500]}"
            ),
            status_code=response.status_code,
        )

    import io
    import zipfile

    local_logger.info("Local MinerU /file_parse completed, extracting ZIP")
    try:
        with zipfile.ZipFile(io.BytesIO(response.content)) as extracted_zip:
            extracted_zip.extractall(output_dir)
    except zipfile.BadZipFile as exc:
        local_logger.bind(error_type=type(exc).__name__).error(
            "Local MinerU response was not a valid ZIP"
        )
        raise MinerUServiceException(
            internal_message=f"Local MinerU returned a non-ZIP body: {exc}",
            original_exception=exc,
        ) from exc

    _flatten_extracted_zip(output_dir)
    local_logger.info("Local MinerU parse completed and ZIP flattened")


def parse_via_full(
    pdf_url: str,
    filename: str,
    output_dir: str,
    s3_key: Optional[str] = None,
) -> None:
    if settings.MINERU_LOCAL_MODE:
        mineru_logger("ingestion_mode", mode="local").info(
            "Using local MinerU mode for ingestion"
        )
        parse_via_local(pdf_url=pdf_url, filename=filename, output_dir=output_dir)
        return

    batch_id: str | None = None
    token_id: str | None = None
    resolved_s3_key = resolve_mineru_source_s3_key(
        s3_key=s3_key,
        local_file_path=None if is_remote(pdf_url) else pdf_url,
    )

    if resolved_s3_key is not None:
        try:
            presigned = JobFileStorage().generate_upload_download_url(
                resolved_s3_key, expires_in=settings.MINERU_URL_MODE_PRESIGN_EXPIRY
            )
            presigned_url = presigned["download_url"]
            mineru_logger("ingestion_mode", mode="s3_url").info(
                "Using S3 URL mode for MinerU ingestion"
            )
            batch_id, token_id = _submit_url_task(presigned_url, filename)
        except Exception as exc:
            _log_mineru_url_mode_ingestion_fallback(
                operation="start_url_mode_ingestion",
                s3_key=resolved_s3_key,
                pdf_url=pdf_url,
                exc=exc,
            )
            resolved_s3_key = None

    if resolved_s3_key is None:
        mineru_logger("ingestion_mode", mode="direct_upload").info(
            "Using direct upload mode for MinerU ingestion"
        )
        batch_id, upload_url, token_id = _request_upload_target(pdf_url, filename)
        _upload_file_to_mineru(pdf_url, filename, upload_url, token_id)

    if batch_id is None or token_id is None:
        raise MinerUServiceException(
            internal_message="MinerU task setup completed without a batch id or token"
        )

    poll_mineru_task(
        status_url=f"{settings.MINERU_URL}/extract-results/batch/{batch_id}",
        task_id=batch_id,
        output_dir=output_dir,
        get_status=get_batch_status,
        preferred_token_id=token_id,
    )
