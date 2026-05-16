# pyright: reportUnusedExpression=false
import os
import time
from typing import Any, Callable, Optional

import requests
from app.services.document_parser.mineru_quota_manager import get_mineru_quota_manager
from app.services.document_parser.parser_log_utils import truncate_log_value
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from shared.core.config import settings
from shared.core.constants import APIConstants
from shared.core.exceptions.domain_exceptions import (
    MinerUServiceException,
    PDFParsingException,
    StorageServiceException,
    TimeoutException,
    UnavailableException,
)
from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.utils.file_loading import is_remote
from shared.utils.zip_download import download_and_extract_zip

MINERU_UPLOAD_TIMEOUT = (
    settings.MINERU_UPLOAD_CONNECT_TIMEOUT,
    settings.MINERU_UPLOAD_READ_TIMEOUT,
)


def _build_mineru_session() -> requests.Session:
    session = requests.Session()
    # Hybrid rate-limit control: urllib3 handles transient 429s with backoff
    # (respects Retry-After header); application-level handling in callers
    # covers persistent rate limits with Redis token marking and pool rotation.
    retry_strategy = Retry(
        total=settings.MINERU_UPLOAD_RETRY_TOTAL,
        backoff_factor=settings.MINERU_UPLOAD_RETRY_BACKOFF_FACTOR,
        status_forcelist=[429, 502, 503, 504],
        allowed_methods=["GET", "POST", "PUT"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=1,
        pool_maxsize=settings.MINERU_POOL_MAXSIZE,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_mineru_session: Optional[requests.Session] = None


def get_mineru_session() -> requests.Session:
    global _mineru_session
    if _mineru_session is None:
        _mineru_session = _build_mineru_session()
    return _mineru_session


def get_mineru_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def _mineru_logger(step: str, **fields: Any):
    return logger.bind(service="mineru", step=step, **fields)


def _should_use_mineru_s3_url_mode(s3_key: Optional[str]) -> bool:
    if settings.FORCE_MINERU_UPLOAD_ENABLED:
        return False

    return settings.ENVIRONMENT != "development" and s3_key is not None


def _log_mineru_url_mode_storage_fallback(
    operation: str,
    s3_key: str,
    local_file_path: Optional[str],
    exc: Exception,
) -> None:
    _mineru_logger(
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
    _mineru_logger(
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
    from app.services.storage.sync_storage_service import verify_s3_file_exists

    try:
        existing_file = verify_s3_file_exists(s3_key, settings.S3_BUCKET_NAME)
    except Exception as exc:
        _log_mineru_url_mode_storage_fallback(
            operation="verify_source_object",
            s3_key=s3_key,
            local_file_path=None,
            exc=exc,
        )
        return None, False

    if existing_file.get("exists"):
        _mineru_logger(
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
    from app.services.storage.sync_storage_service import upload_to_s3

    try:
        upload_to_s3(local_file_path, s3_key, settings.S3_BUCKET_NAME)
    except Exception as exc:
        _log_mineru_url_mode_storage_fallback(
            operation="upload_source_object",
            s3_key=s3_key,
            local_file_path=local_file_path,
            exc=exc,
        )
        return None

    _mineru_logger(
        "url_mode_source_uploaded",
        source_s3_key=s3_key,
        local_file_path=local_file_path,
    ).info("Uploaded local PDF to S3 for MinerU URL mode")
    return s3_key


def _get_retry_after_seconds(
    response: requests.Response, default_retry_after: int
) -> int:
    """Parse Retry-After header with sane bounds for worker backoff."""
    retry_after_header = response.headers.get("Retry-After")
    if retry_after_header:
        try:
            return max(
                1,
                min(
                    int(retry_after_header), settings.MINERU_RATE_LIMIT_MAX_RETRY_AFTER
                ),
            )
        except ValueError:
            logger.debug(f"Invalid MinerU Retry-After header: {retry_after_header}")

    return max(1, min(default_retry_after, settings.MINERU_RATE_LIMIT_MAX_RETRY_AFTER))


def _raise_mineru_unavailable(
    token_id: str, response: requests.Response, operation: str
) -> None:
    retry_after = _get_retry_after_seconds(
        response, settings.MINERU_TOKEN_COOLDOWN_SECONDS
    )
    quota_manager = get_mineru_quota_manager()
    quota_manager.mark_rate_limited(token_id, retry_after)
    _mineru_logger(
        "rate_limited",
        operation=operation,
        token_id=token_id,
        status_code=response.status_code,
        retry_after=retry_after,
    ).warning("MinerU request rate-limited")
    raise UnavailableException(
        internal_message=f"MinerU rate limited during {operation}",
        retry_after=retry_after,
        limit=settings.MINERU_TOKEN_RPM_LIMIT,
        period="minute",
        user_message="Document processing is busy right now. Please retry shortly.",
    )


def _polling_interval_for_state(state: str, attempt: int) -> float:
    """Return seconds to sleep before the next poll.

    Tuned so that ``WORKER_CONCURRENCY`` scales safely with MinerU limits.
    With a 4-token pool (300 RPM each), total budget is 1200 req/min:

        150 tasks × (60 s / 15 s) = 600 req/min total
        600 / 4 tokens = 150 req/min per token   ← leaves headroom

    Observed data (Logfire, 2026-03-08 dev batch):
    - 99 % of tasks never enter ``running``; lifecycle is
      ``waiting-file`` → ``done`` in 2-4 s on MinerU's side.
    - Longest observed task: 21 s (5 poll attempts).
    - Peak burst: 351 concurrent tasks, 308 req/min → rate-limited.
    """
    if state == "pending":
        return min(20.0, 5.0 + attempt * 1.5)
    if state == "running":
        return 10.0
    # waiting-file, converting, unknown, etc.
    return 15.0


def _get_batch_status(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    extract_result = data.get("data", {}).get("extract_result")
    if isinstance(extract_result, list):
        return extract_result[0] if extract_result else None
    return extract_result


def poll_mineru_task(
    status_url: str,
    task_id: str,
    output_dir: str,
    get_status: Callable[[dict[str, Any]], Optional[dict[str, Any]]],
    preferred_token_id: Optional[str] = None,
) -> None:
    quota_manager = get_mineru_quota_manager()
    polling_logger = _mineru_logger(
        "poll_status",
        operation="poll_status",
        task_id=task_id,
        preferred_token_id=preferred_token_id,
    )

    max_polling_attempts = 120
    polling_interval = 5.0
    max_wait_time = 6000

    start_time = time.time()
    attempt = 0
    last_token_id: Optional[str] = None
    last_state: Optional[str] = None

    polling_logger.info("Starting MinerU polling")

    while attempt < max_polling_attempts:
        if time.time() - start_time > max_wait_time:
            polling_logger.bind(
                attempt=attempt + 1,
                max_polling_attempts=max_polling_attempts,
                max_wait_time=max_wait_time,
            ).warning("MinerU polling timed out")
            raise TimeoutException(
                internal_message=f"PDF parsing timed out, exceeded {max_wait_time} seconds",
                retry_after=60,
                user_message="PDF parsing timed out. Please try again.",
            )

        try:
            logger.debug(
                f"parse_pdfs status_url: {status_url} "
                f"(attempt {attempt + 1}/{max_polling_attempts})"
            )
            lease = quota_manager.acquire_request(
                operation="poll_status",
                preferred_token_id=preferred_token_id,
            )
            if lease.token_id != last_token_id:
                polling_logger.bind(
                    token_id=lease.token_id,
                    attempt=attempt + 1,
                ).info("Acquired MinerU token for polling")
                last_token_id = lease.token_id

            response = get_mineru_session().get(
                status_url,
                headers=get_mineru_headers(lease.api_key),
                timeout=settings.MINERU_API_TIMEOUT,
            )

            if response.status_code == 429:
                # urllib3 already retried with backoff — if we still see 429,
                # mark the token and let Celery handle task-level retry.
                _raise_mineru_unavailable(
                    lease.token_id, response, operation="poll_status"
                )

            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("code") != 0:
                    response_message = str(response_json.get("msg") or "Unknown error")
                    if "rate limit" in response_message.lower():
                        quota_manager.mark_rate_limited(
                            lease.token_id,
                            settings.MINERU_TOKEN_COOLDOWN_SECONDS,
                        )
                        raise UnavailableException(
                            internal_message=(
                                f"MinerU rate limited during poll_status: {response_message}"
                            ),
                            retry_after=settings.MINERU_TOKEN_COOLDOWN_SECONDS,
                            limit=lease.rpm_limit,
                            period="minute",
                            user_message="Document processing is busy right now. Please retry shortly.",
                        )
                    raise MinerUServiceException(
                        internal_message=f"MinerU API Error: {response_message}"
                    )

                status = get_status(response_json)
                if not status:
                    polling_logger.bind(
                        token_id=lease.token_id,
                        attempt=attempt + 1,
                    ).warning("Received empty MinerU status payload")
                    time.sleep(polling_interval)
                    attempt += 1
                    continue

                state = status.get("state", "unknown")
                if state != last_state:
                    # polling_logger.bind(
                    #     token_id=lease.token_id,
                    #     attempt=attempt + 1,
                    #     state=state,
                    # ).info("MinerU status changed")
                    last_state = state

                if state == "done":
                    download_and_extract_zip(
                        status["full_zip_url"],
                        dest_dir=output_dir,
                        keep_exts=(".md", ".jpg", ".jpeg", ".png", ".gif", ".json"),
                        exclude_patterns=("content_list", "middle.json", "model.json"),
                    )
                    polling_logger.bind(token_id=lease.token_id).info(
                        "MinerU parsing completed"
                    )
                    break

                if state == "running":
                    if "extract_progress" in status:
                        try:
                            (
                                status["extract_progress"]["extracted_pages"]
                                / status["extract_progress"]["total_pages"]
                            )
                            # polling_logger.bind(
                            #     token_id=lease.token_id,
                            #     progress=progress,
                            # ).info("MinerU parsing progress updated")
                        except (KeyError, ZeroDivisionError):
                            polling_logger.bind(token_id=lease.token_id).info(
                                "MinerU parsing in progress"
                            )
                    else:
                        polling_logger.bind(token_id=lease.token_id).info(
                            "MinerU parsing in progress"
                        )
                elif state == "failed":
                    error_message = status.get("err_msg", "Unknown error")
                    polling_logger.bind(
                        token_id=lease.token_id,
                        error_message=error_message,
                    ).error("MinerU parsing reported failed state")
                    raise PDFParsingException(
                        user_message="Failed to parse the PDF file",
                        internal_message=f"MinerU failed with state 'failed': {error_message}",
                    )
                elif state == "pending":
                    polling_logger.bind(token_id=lease.token_id).debug(
                        "MinerU parsing pending"
                    )
                elif state == "waiting-file":
                    polling_logger.bind(token_id=lease.token_id).debug(
                        "MinerU waiting for file queueing"
                    )
                elif state == "converting":
                    polling_logger.bind(token_id=lease.token_id).debug(
                        "MinerU converting file"
                    )
                else:
                    polling_logger.bind(
                        token_id=lease.token_id,
                        state=state,
                    ).warning("MinerU returned unknown state")

                time.sleep(_polling_interval_for_state(state, attempt))
                attempt += 1
            else:
                polling_logger.bind(
                    token_id=lease.token_id,
                    attempt=attempt + 1,
                    status_code=response.status_code,
                ).warning("MinerU status query failed")
                time.sleep(polling_interval * 2)
                attempt += 1

        except requests.RequestException as exc:
            polling_logger.bind(
                attempt=attempt + 1,
                error_message=str(exc),
            ).warning("MinerU polling network request failed")
            time.sleep(polling_interval * 2)
            attempt += 1
        except KnowhereException:
            raise
        except Exception as exc:
            polling_logger.bind(
                attempt=attempt + 1,
                error_message=str(exc),
            ).error("Unexpected error during MinerU polling")
            raise PDFParsingException(
                user_message="An unexpected error occurred while parsing the PDF",
                internal_message=str(exc),
                original_exception=exc,
            )

    if attempt >= max_polling_attempts:
        raise TimeoutException(
            internal_message=(
                f"minerU PDF parsing timed out after {max_polling_attempts} attempts, "
                f"Task ID: {task_id}"
            ),
            retry_after=60,
            user_message="PDF parsing timed out. Please try again.",
        )


def _request_upload_target(pdf_url: str, filename: str) -> tuple[str, str, str]:
    base_url = settings.MINERU_URL
    quota_manager = get_mineru_quota_manager()
    upload_logger = _mineru_logger(
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
        _raise_mineru_unavailable(lease.token_id, response, operation="upload_url")
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
    upload_logger = _mineru_logger(
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
    submit_logger = _mineru_logger(
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
        _raise_mineru_unavailable(lease.token_id, response, operation="submit_url_task")

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


def parse_via_full(
    pdf_url: str,
    filename: str,
    output_dir: str,
    s3_key: Optional[str] = None,
) -> None:
    resolved_s3_key = resolve_mineru_source_s3_key(
        s3_key=s3_key,
        local_file_path=None if is_remote(pdf_url) else pdf_url,
    )

    if resolved_s3_key is not None:
        try:
            from app.services.storage.sync_storage_service import generate_download_url

            presigned = generate_download_url(
                resolved_s3_key, expires_in=settings.MINERU_URL_MODE_PRESIGN_EXPIRY
            )
            presigned_url = presigned["download_url"]
            _mineru_logger("ingestion_mode", mode="s3_url").info(
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
        _mineru_logger("ingestion_mode", mode="direct_upload").info(
            "Using direct upload mode for MinerU ingestion"
        )
        batch_id, upload_url, token_id = _request_upload_target(pdf_url, filename)
        _upload_file_to_mineru(pdf_url, filename, upload_url, token_id)

    poll_mineru_task(
        status_url=f"{settings.MINERU_URL}/extract-results/batch/{batch_id}",
        task_id=batch_id,
        output_dir=output_dir,
        get_status=_get_batch_status,
        preferred_token_id=token_id,
    )
