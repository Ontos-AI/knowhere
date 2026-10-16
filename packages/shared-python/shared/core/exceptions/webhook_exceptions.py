"""
Webhook-specific exceptions.

These exceptions are used internally for webhook delivery logic and
are not typically exposed to API clients directly.
"""

from typing import Any, Dict, Optional

from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.core.response.ErrorCode import ErrorCode


class WebhookException(KnowhereException):
    """Base exception for all webhook-related errors."""

    pass


class WebhookConfigException(WebhookException):
    """
    Invalid webhook configuration (400).

    Raised when:
    - Missing or invalid webhook URL
    - Missing or invalid webhook secret
    - URL scheme not allowed (must be https in production)
    """

    def __init__(
        self,
        internal_message: str,
        user_message: str = "Invalid webhook configuration.",
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            code=ErrorCode.INVALID_ARGUMENT,
            internal_message=internal_message,
            user_message=user_message,
            http_status_code=400,
            details=details,
        )


class WebhookDeliveryException(WebhookException):
    """
    Webhook delivery failure (500).

    Raised when:
    - HTTP request to webhook URL fails
    - Response status code is non-2xx
    - Network timeout occurs

    The retryable flag indicates whether this failure is transient
    and the delivery should be retried.
    """

    def __init__(
        self,
        internal_message: str,
        retryable: bool = True,
        status_code: Optional[int] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message="Webhook delivery failed.",
            http_status_code=500,
        )
        self.retryable = retryable
        self.response_status_code = status_code
