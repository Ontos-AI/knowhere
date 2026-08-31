"""
Canonical Error Codes for the Knowhere API.

These codes are the source of truth for all API error responses.
They follow the gRPC error model mapped to HTTP status codes.

Usage:
    from shared.core.response.ErrorCode import ErrorCode

    raise KnowhereException(
        code=ErrorCode.INVALID_ARGUMENT,
        message="The file format is not supported."
    )

Retry Semantics:
    Some error codes are always retryable (UNAVAILABLE, DEADLINE_EXCEEDED).
    Others are conditionally retryable based on `details`:

    RESOURCE_EXHAUSTED (429):
        - Rate Limit: details.retry_after = 15  → RETRY after delay
        - Quota Exceeded: details.retry_after = None → NO RETRY (upgrade plan)

    Client Logic:
        if error.code == "RESOURCE_EXHAUSTED":
            if error.details.get("retry_after"):
                time.sleep(error.details["retry_after"])
                retry_request()
            else:
                show_upgrade_prompt()  # Quota exhausted
"""

from enum import Enum


class ErrorCode(str, Enum):
    """
    Canonical error codes that determine the HTTP status and client retry strategy.

    Guidelines:
    - Always use the canonical code for the top-level 'code' field.
    - Do NOT create infrastructure-specific codes (e.g., REDIS_ERROR).
    - Use the 'details' field for sub-codes or additional context.
    """

    # Success
    OK = "OK"

    # Client Errors (4xx)
    INVALID_ARGUMENT = "INVALID_ARGUMENT"  # 400 - Bad input from client
    FAILED_PRECONDITION = "FAILED_PRECONDITION"  # 400 - System state prevents operation
    OUT_OF_RANGE = "OUT_OF_RANGE"  # 400 - Value outside valid range
    UNAUTHENTICATED = "UNAUTHENTICATED"  # 401 - Missing/invalid credentials
    PAYMENT_REQUIRED = (
        "PAYMENT_REQUIRED"  # 402 - Payment required / Insufficient credits
    )
    PERMISSION_DENIED = "PERMISSION_DENIED"  # 403 - Caller lacks permission
    NOT_FOUND = "NOT_FOUND"  # 404 - Resource does not exist
    ABORTED = "ABORTED"  # 409 - Concurrency conflict
    ALREADY_EXISTS = "ALREADY_EXISTS"  # 409 - Resource already exists
    RESOURCE_EXHAUSTED = (
        "RESOURCE_EXHAUSTED"  # 429 - Rate limit OR quota (see Retry Semantics)
    )
    CANCELLED = "CANCELLED"  # 499 - Client cancelled request

    # Server Errors (5xx)
    UNKNOWN = "UNKNOWN"  # 500 - Unknown error (fallback)
    INTERNAL_ERROR = "INTERNAL_ERROR"  # 500 - Internal invariants broken
    DATA_LOSS = "DATA_LOSS"  # 500 - Unrecoverable data loss
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"  # 501 - Method not implemented
    UNAVAILABLE = "UNAVAILABLE"  # 503 - Service unavailable (retry)
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"  # 504 - Timeout


class ErrorCodeMapper:
    """
    Bidirectional mapping between ErrorCode and HTTP status codes.

    Usage:
        ErrorCodeMapper.get_http_status_from_error_code(ErrorCode.NOT_FOUND)  # 404
        ErrorCodeMapper.get_error_code_from_http_status(401)  # ErrorCode.UNAUTHENTICATED
    """

    _error_code_to_http_status = {
        ErrorCode.OK: 200,
        ErrorCode.INVALID_ARGUMENT: 400,
        ErrorCode.FAILED_PRECONDITION: 400,
        ErrorCode.OUT_OF_RANGE: 400,
        ErrorCode.UNAUTHENTICATED: 401,
        ErrorCode.PAYMENT_REQUIRED: 402,
        ErrorCode.PERMISSION_DENIED: 403,
        ErrorCode.NOT_FOUND: 404,
        ErrorCode.ABORTED: 409,
        ErrorCode.ALREADY_EXISTS: 409,
        ErrorCode.RESOURCE_EXHAUSTED: 429,
        ErrorCode.CANCELLED: 499,
        ErrorCode.UNKNOWN: 500,
        ErrorCode.INTERNAL_ERROR: 500,
        ErrorCode.DATA_LOSS: 500,
        ErrorCode.NOT_IMPLEMENTED: 501,
        ErrorCode.UNAVAILABLE: 503,
        ErrorCode.DEADLINE_EXCEEDED: 504,
    }

    # Reverse mapping: HTTP Status -> ErrorCode
    # Note: Some statuses map to multiple codes; we pick the most common one.
    _http_status_to_error_code = {
        200: ErrorCode.OK,
        400: ErrorCode.INVALID_ARGUMENT,
        401: ErrorCode.UNAUTHENTICATED,
        402: ErrorCode.PAYMENT_REQUIRED,
        403: ErrorCode.PERMISSION_DENIED,
        404: ErrorCode.NOT_FOUND,
        409: ErrorCode.ALREADY_EXISTS,
        422: ErrorCode.INVALID_ARGUMENT,  # Pydantic validation
        429: ErrorCode.RESOURCE_EXHAUSTED,
        499: ErrorCode.CANCELLED,
        500: ErrorCode.INTERNAL_ERROR,
        501: ErrorCode.NOT_IMPLEMENTED,
        502: ErrorCode.UNAVAILABLE,
        503: ErrorCode.UNAVAILABLE,
        504: ErrorCode.DEADLINE_EXCEEDED,
    }

    @classmethod
    def get_http_status_from_error_code(
        cls, code: ErrorCode, default: int = 500
    ) -> int:
        """Get HTTP status code from ErrorCode."""
        return cls._error_code_to_http_status.get(code, default)

    @classmethod
    def get_error_code_from_http_status(
        cls, status: int, default: ErrorCode = ErrorCode.UNKNOWN
    ) -> ErrorCode:
        """Get ErrorCode from HTTP status code."""
        return cls._http_status_to_error_code.get(status, default)


# Always Retryable Error Codes (retry with exponential backoff)
# Note: RESOURCE_EXHAUSTED is NOT in this set - check details.retry_after
ALWAYS_RETRYABLE_ERROR_CODES = frozenset(
    [
        ErrorCode.ABORTED,  # Concurrency conflict - retry immediately
        ErrorCode.UNAVAILABLE,  # Service down - retry with backoff
        ErrorCode.DEADLINE_EXCEEDED,  # Timeout - retry with backoff
    ]
)

# Conditionally Retryable - check `details` field
# RESOURCE_EXHAUSTED: Retry ONLY if details.retry_after is present


class SubCode(str, Enum):
    """
    Client-facing sub-codes for use in `details.reason`.
    These provide actionable context to the client.

    NOTE: Internal debugging info (e.g., REDIS_CONNECTION_FAILED) should NOT
    be here. Use the Exception object's internal fields for logging instead.
    """

    # RESOURCE_EXHAUSTED sub-codes
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    CONCURRENT_LIMIT_EXCEEDED = "CONCURRENT_LIMIT_EXCEEDED"

    # INVALID_ARGUMENT sub-codes
    MALFORMED_INPUT = "MALFORMED_INPUT"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"

    # NOT_FOUND sub-codes
    USER_NOT_FOUND = "USER_NOT_FOUND"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
