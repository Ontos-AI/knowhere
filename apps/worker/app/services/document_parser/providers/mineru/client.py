from __future__ import annotations

from typing import Any, Optional

import requests
from app.services.document_parser.providers.mineru.quota_manager import get_mineru_quota_manager
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import UnavailableException


def build_mineru_session() -> requests.Session:
    session = requests.Session()
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
        _mineru_session = build_mineru_session()
    return _mineru_session


def get_mineru_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def mineru_logger(step: str, **fields: Any):
    return logger.bind(service="mineru", step=step, **fields)


def get_retry_after_seconds(
    response: requests.Response, default_retry_after: int
) -> int:
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


def raise_mineru_unavailable(
    token_id: str, response: requests.Response, operation: str
) -> None:
    retry_after = get_retry_after_seconds(
        response, settings.MINERU_TOKEN_COOLDOWN_SECONDS
    )
    quota_manager = get_mineru_quota_manager()
    quota_manager.mark_rate_limited(token_id, retry_after)
    mineru_logger(
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
