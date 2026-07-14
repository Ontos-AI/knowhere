"""
Base Exception class for the Knowhere API.

=============================================================================
SECURITY RULE: THE "4xx vs 5xx" MESSAGE PATTERN
=============================================================================

This class implements a dual-message pattern for security and developer experience:

    - `internal_message`: Technical details for LOGS ONLY. NEVER sent to client.
    - `user_message`:     Safe message for CLIENT. ALWAYS sent to user.

For 5xx (System Errors):
    - Developer provides `internal_message` for debugging (e.g., "Disk full on /mnt/data")
    - System auto-generates safe `user_message` (e.g., "Internal system error...")
    - Developer CAN override `user_message` for specific safe messages

For 4xx (Client Errors):
    - Developer provides `user_message` that helps user fix their input
    - `internal_message` is optional (for extra debugging context)

=============================================================================
WARNING: DO NOT RAISE THIS CLASS DIRECTLY IN YOUR CODE.
=============================================================================

This is an abstract base class. Always use the domain-specific exceptions
from `shared.core.exceptions.domain_exceptions`:

    - ValidationException  (400 - invalid input with violations)
    - AuthException        (401 - authentication failed)
    - PermissionException  (403 - permission denied)
    - NotFoundException    (404 - resource not found)
    - RateLimitException   (429 - rate limit, retryable)
    - QuotaExceededException (429 - quota, NOT retryable)
    - UnavailableException (503 - service down, retryable)
    - TimeoutException     (504 - timeout, retryable)
    - UnknownException     (500 - wrap unexpected errors)

Correct Usage (4xx - Client Error):
    from shared.core.exceptions import ValidationException

    raise ValidationException(
        user_message="The file 'data.csv' is too large (max 5MB).",
        violations=[{"field": "file", "description": "Exceeds 5MB limit"}]
    )

Correct Usage (5xx - System Error):
    from shared.core.exceptions import FileSystemException

    raise FileSystemException(
        internal_message="Permission denied: cannot write to /var/lib/worker/tmp",
        operation="write"
    )
    # User sees: "An internal system error occurred. Please contact support."
    # Logs see: "Permission denied: cannot write to /var/lib/worker/tmp"

Wrong Usage:
    # DO NOT DO THIS:
    raise KnowhereException(code=ErrorCode.INVALID_ARGUMENT, ...)
"""

from collections.abc import Mapping
from typing import Any, Dict, Literal, Optional

from shared.core.response.ErrorCode import ErrorCode, ErrorCodeMapper

# Default messages for auto-sanitization
DEFAULT_5XX_USER_MESSAGE = "An internal system error occurred. Please contact support."
DEFAULT_4XX_USER_MESSAGE = "Invalid request. Please check your input."
ErrorCategory = Literal["client", "system"]


class KnowhereException(Exception):
    """
    Abstract base class for all Knowhere API exceptions.

    ==========================================================================
    SECURITY: THE DUAL-MESSAGE PATTERN
    ==========================================================================

    This class enforces a strict separation between:

    1. `internal_message` - Technical details for DEBUGGING (logs only)
       - Contains specific error info (paths, IDs, stack traces)
       - NEVER exposed to the client (security risk)
       - Used by developers/ops to diagnose issues

    2. `user_message` - Safe message for the CLIENT
       - Contains user-friendly, actionable information
       - ALWAYS returned in API response
       - For 5xx: Auto-defaults to generic safe message
       - For 4xx: Developer must provide helpful message

    ==========================================================================

    WARNING: DO NOT INSTANTIATE OR RAISE THIS CLASS DIRECTLY.
    Use domain-specific subclasses from domain_exceptions.py instead.

    This class provides:
    - to_client(): Machine-readable JSON for API responses (user_message only)
    - to_log(): Structured dict for log aggregators (Datadog, Splunk, CloudWatch)
    - __repr__(): Human-readable string for terminal logs (tail -f server.log)

    Adheres to the 3 Rules + Security:
    1. Be Explicit - Use specific domain exceptions
    2. Machine Readable - to_client() returns consistent JSON schema
    3. Security First - internal_message NEVER in response; only user_message

    Attributes:
        code: Canonical error code (determines HTTP status)
        user_message: Safe, user-friendly message (ALWAYS sent to client)
        internal_message: Technical details for logging (NEVER sent to client)
        details: Client-facing structured data (e.g., retry_after, reason)
        http_status_code: HTTP status (auto-derived from code if not specified)
        original_exception: Wrapped exception for logging (NOT sent to client)
    """

    def __init__(
        self,
        code: ErrorCode,
        internal_message: str,
        user_message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        http_status_code: Optional[int] = None,
        original_exception: Optional[Exception] = None,
        error_category: ErrorCategory | None = None,
        exception_context: Mapping[str, object] | None = None,
    ):
        """
        Initialize a KnowhereException.

        Args:
            code: The canonical ErrorCode for this exception.
            internal_message: Technical message for logs. NEVER sent to client.
            user_message: Safe message for client. Auto-defaults based on HTTP status:
                - 5xx: Defaults to "An internal system error occurred..."
                - 4xx: Defaults to "Invalid request. Please check your input."
            details: Optional structured data to include in response (must be safe).
            http_status_code: Override HTTP status (auto-derived from code if None).
            original_exception: The underlying exception being wrapped (for logging).
            error_category: Optional telemetry category override. Defaults to the
                category derived from the HTTP status.
            exception_context: Internal-only structured telemetry fields. These are
                included in logs and never returned to clients.
        """
        super().__init__(internal_message)
        self.code = code
        self.internal_message = internal_message
        self.details = details or {}
        self.http_status_code = (
            http_status_code or ErrorCodeMapper.get_http_status_from_error_code(code)
        )
        self.original_exception = original_exception
        default_error_category: ErrorCategory = (
            "system" if self.http_status_code >= 500 else "client"
        )
        self.error_category: ErrorCategory = error_category or default_error_category
        self.exception_context: Dict[str, object] = dict(exception_context or {})

        # =======================================================================
        # SECURITY: Auto-sanitize user_message based on HTTP status
        # =======================================================================
        # For 5xx (system errors): Default to generic safe message
        # For 4xx (client errors): Default to generic safe message
        # Domain exceptions SHOULD always provide explicit user_message
        if self.http_status_code >= 500:
            self.user_message = user_message or DEFAULT_5XX_USER_MESSAGE
        else:
            self.user_message = user_message or DEFAULT_4XX_USER_MESSAGE

    def to_client(self, request_id: str) -> Dict[str, Any]:
        """
        Returns a machine-readable JSON representation for API responses.

        SECURITY: This method returns `user_message`, NEVER `internal_message`.
        This is the ONLY data that gets sent to the client.
        """
        response: Dict[str, Any] = {
            "success": False,
            "error": {
                "code": self.code.value,
                "message": self.user_message,  # SECURITY: Only user_message exposed
                "request_id": request_id,
            },
        }
        # Only include details if non-empty
        if self.details:
            response["error"]["details"] = self.details
        return response

    def __repr__(self) -> str:
        """
        Human-readable string for terminal logs (tail -f server.log).

        Use this in logger calls: logger.error(f"Error: {exc}")
        """
        parts = [
            f"{self.__class__.__name__}(",
            f"code={self.code.value!r}",
            f"http_status={self.http_status_code}",
            f"user_message={self.user_message!r}",
            f"internal_message={self.internal_message!r}",
        ]
        if self.details:
            parts.append(f"details={self.details!r}")
        if self.original_exception:
            parts.append(
                f"original={type(self.original_exception).__name__}: {self.original_exception}"
            )
        return ", ".join(parts) + ")"

    def to_log(self) -> Dict[str, Any]:
        """
        Machine-readable dict for structured logging (Datadog, Splunk, CloudWatch).

        Use with logger.bind(): logger.bind(**exc.to_log()).error("...")

        This enables log aggregators to index fields for queries like:
            code:INVALID_ARGUMENT AND http_status:400

        Returns stable error fields for exception logs:
            - error_code: Canonical error code
            - http_status: HTTP status code
            - error_category: "client" (4xx) or "system" (5xx)
            - exception_class: Exception class name
            - internal_message: Technical details for debugging
            - user_message: Safe message for client
            - details: Additional structured data
            - original_exception: Wrapped exception info
        """
        log_data: Dict[str, Any] = {
            **self.exception_context,
            "error_code": self.code.value,
            "http_status": self.http_status_code,
            "error_category": self.error_category,
            "exception_class": self.__class__.__name__,
            "internal_message": self.internal_message,
            "user_message": self.user_message,
        }
        if self.details:
            log_data["details"] = self.details
        if self.original_exception:
            log_data["original_exception"] = {
                "type": type(self.original_exception).__name__,
                "message": str(self.original_exception),
            }
        return log_data

    def logging(self, **extra_context):
        """
        Canonical logging method - context automatic.

        This method automatically:
        - Reads current log context (request_id, task_id, job_id, etc.)
        - Merges exception fields from to_log()
        - Logs at appropriate level (ERROR for 5xx, WARNING for 4xx)
        - Includes stacktrace for 5xx errors
        - Ensures user_message is present in exception logs

        Usage:
            try:
                # ... operation that fails ...
            except Exception as e:
                exc = ValidationException(
                    user_message="Invalid input",
                    internal_message="Field validation failed"
                )
                exc.logging()  # Context automatic - includes request_id, etc.
                raise exc

        Args:
            **extra_context: Additional context fields to include in log
        """
        from loguru import logger

        from shared.core.logging import LogEvent, get_log_context

        # Get current context (request_id, task_id, job_id, etc.)
        context = get_log_context()

        # Build log data with stable error fields
        log_data = {
            **self.to_log(),
            **context,
            **extra_context,
        }

        # Log at appropriate level with appropriate event
        if self.error_category == "system":
            # System-category errors use ERROR level with a stacktrace even when
            # their public HTTP status intentionally remains a 4xx response.
            logger.bind(event=LogEvent.EXCEPTION_SYSTEM.value, **log_data).opt(
                exception=self
            ).error(self.internal_message)
        else:
            # 4xx: WARNING level without stacktrace
            logger.bind(event=LogEvent.EXCEPTION_CLIENT.value, **log_data).warning(
                self.internal_message
            )

    def __reduce__(self):
        """
        Enable pickle serialization for Celery task exception handling.

        Celery uses pickle to serialize exceptions for retries and results.
        Without this method, exceptions get wrapped in UnpickleableExceptionWrapper.

        Uses factory function to bypass subclass __init__ signature differences.
        Note: original_exception is NOT serialized (it may contain unpickleable objects).
        """
        return (
            _reconstruct_knowhere_exception,
            (self.__class__, self.__getstate__()),
        )

    def __getstate__(self):
        """Return state for pickling (excludes unpickleable original_exception)."""
        state = self.__dict__.copy()
        # original_exception may contain unpickleable objects
        state["original_exception"] = None
        return state

    def __setstate__(self, state):
        """Restore state from pickle."""
        self.__dict__.update(state)
        # Re-initialize Exception base class with message
        Exception.__init__(self, self.internal_message)


def _reconstruct_knowhere_exception(cls, state):
    """
    Factory function for unpickling KnowhereException subclasses.

    Bypasses __init__ to handle subclasses with different signatures.
    """
    obj = cls.__new__(cls)
    obj.__setstate__(state)
    return obj
