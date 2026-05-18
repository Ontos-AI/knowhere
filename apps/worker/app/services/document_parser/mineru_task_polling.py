from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Optional

import requests
from app.services.document_parser.mineru_client import (
    get_mineru_headers,
    get_mineru_session,
    mineru_logger,
    raise_mineru_unavailable,
)
from app.services.document_parser.mineru_quota_manager import get_mineru_quota_manager
from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    MinerUServiceException,
    PDFParsingException,
    TimeoutException,
    UnavailableException,
)
from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.services.http.zip_download import download_and_extract_zip


def get_batch_status(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    extract_result = data.get("data", {}).get("extract_result")
    if isinstance(extract_result, list):
        return extract_result[0] if extract_result else None
    return extract_result


def get_polling_interval_for_state(state: str, attempt: int) -> float:
    """Return seconds to sleep before the next MinerU status poll."""
    if state == "pending":
        return min(20.0, 5.0 + attempt * 1.5)
    if state == "running":
        return 10.0
    return 15.0


def poll_mineru_task(
    status_url: str,
    task_id: str,
    output_dir: str,
    get_status: Callable[[dict[str, Any]], Optional[dict[str, Any]]],
    preferred_token_id: Optional[str] = None,
) -> None:
    quota_manager = get_mineru_quota_manager()
    polling_logger = mineru_logger(
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
                raise_mineru_unavailable(
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
                            _progress = (
                                status["extract_progress"]["extracted_pages"]
                                / status["extract_progress"]["total_pages"]
                            )
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

                time.sleep(get_polling_interval_for_state(state, attempt))
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
