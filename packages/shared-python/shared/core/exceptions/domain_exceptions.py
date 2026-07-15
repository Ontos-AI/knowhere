"""
Domain-specific exceptions with typed details schemas.

=============================================================================
SECURITY: THE "4xx vs 5xx" MESSAGE PATTERN
=============================================================================

Each exception follows the dual-message pattern:

    - `internal_message`: Technical details for LOGS ONLY. NEVER sent to client.
    - `user_message`:     Safe message for CLIENT. ALWAYS sent to user.

4xx Exceptions (Client Errors):
    - Developer MUST provide `user_message` (helpful for user to fix their input)
    - `internal_message` is optional (for extra debugging context)

5xx Exceptions (System Errors):
    - Developer provides `internal_message` for debugging
    - `user_message` auto-defaults to generic safe message
    - Developer CAN override with custom safe `user_message`

=============================================================================

Each exception has a fixed `details` structure that clients can rely on.
Do NOT raise KnowhereException directly - use these domain exceptions.

Usage (4xx - Client Error):
    from shared.core.exceptions import ValidationException

    raise ValidationException(
        user_message="The file 'data.csv' is too large (max 5MB).",
        violations=[{"field": "file", "description": "Exceeds 5MB limit"}]
    )

Usage (5xx - System Error):
    from shared.core.exceptions import FileSystemException

    raise FileSystemException(
        internal_message="Permission denied: cannot write to /var/lib/worker/tmp",
        operation="write"
    )
    # User sees: "An internal system error occurred. Please contact support."
"""

from typing import Any, Dict, List, Optional, TypedDict

from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.core.response.ErrorCode import ErrorCode, SubCode

# ============================================================================
# Type Definitions for Details Schemas
# ============================================================================


class Violation(TypedDict):
    """Schema for validation violations."""

    field: str
    description: str


class ResourceInfo(TypedDict):
    """Schema for resource identification."""

    resource: str
    id: str


# ============================================================================
# Client Error Exceptions (4xx)
# ----------------------------------------------------------------------------
# For 4xx errors, developer MUST provide `user_message` that helps the user
# understand and fix their input error. `internal_message` is optional.
# ============================================================================


class ValidationException(KnowhereException):
    """
    Invalid input from client. HTTP 400.

    4xx Error: Developer provides `user_message` that user sees directly.

    Details schema:
        {"violations": [{"field": "...", "description": "..."}]}
    """

    def __init__(
        self,
        user_message: str,
        violations: List[Violation],
        internal_message: Optional[str] = None,
    ):
        super().__init__(
            code=ErrorCode.INVALID_ARGUMENT,
            internal_message=internal_message or user_message,
            user_message=user_message,
            details={"violations": violations},
        )
        self.violations = violations


class AuthException(KnowhereException):
    """
    Authentication failed. HTTP 401.

    4xx Error: Developer provides `user_message` that user sees directly.

    Details schema: {} (no additional details for security)
    """

    def __init__(
        self,
        user_message: str = "Authentication required",
        internal_message: Optional[str] = None,
    ):
        super().__init__(
            code=ErrorCode.UNAUTHENTICATED,
            internal_message=internal_message or user_message,
            user_message=user_message,
            details={},  # Empty for security
        )


class InsufficientCreditsException(KnowhereException):
    """
    Payment required. HTTP 402.

    4xx Error: Developer provides `user_message` that user sees directly.

    Details schema:
        {"required_credits": 10, "current_balance": 5}
    """

    def __init__(
        self,
        user_message: str,
        required_credits: Optional[float] = None,
        current_balance: Optional[float] = None,
        internal_message: Optional[str] = None,
    ):
        details: Dict[str, Any] = {}
        if required_credits is not None:
            details["required_credits"] = required_credits
        if current_balance is not None:
            details["current_balance"] = current_balance

        super().__init__(
            code=ErrorCode.PAYMENT_REQUIRED,
            internal_message=internal_message or user_message,
            user_message=user_message,
            details=details,
        )


class PermissionDeniedException(KnowhereException):
    """
    Permission denied. HTTP 403.

    4xx Error: Developer provides `user_message` that user sees directly.

    Details schema:
        {"required_permission": "..."}
    """

    def __init__(
        self,
        user_message: str = "Permission denied",
        required_permission: Optional[str] = None,
        internal_message: Optional[str] = None,
    ):
        details: Dict[str, Any] = {}
        if required_permission:
            details["required_permission"] = required_permission
        super().__init__(
            code=ErrorCode.PERMISSION_DENIED,
            internal_message=internal_message or user_message,
            user_message=user_message,
            details=details,
        )


class NotFoundException(KnowhereException):
    """
    Resource not found. HTTP 404.

    4xx Error: Auto-generates user_message from resource name.

    Details schema:
        {"resource": "...", "id": "..."}
    """

    def __init__(
        self,
        resource: str,
        resource_id: str,
        internal_message: Optional[str] = None,
    ):
        user_msg = f"{resource} not found"
        super().__init__(
            code=ErrorCode.NOT_FOUND,
            internal_message=internal_message
            or f"{resource} with id={resource_id} not found",
            user_message=user_msg,
            details={"resource": resource, "id": resource_id},
        )


class ConflictException(KnowhereException):
    """
    Resource conflict (e.g., already exists, concurrent update). HTTP 409.

    4xx Error: Developer provides `user_message` that user sees directly.

    Details schema:
        {"reason": "ALREADY_EXISTS" | "ABORTED", "resource": "...", "id": "..."}
    """

    def __init__(
        self,
        user_message: str,
        reason: str = "ALREADY_EXISTS",
        resource: Optional[str] = None,
        resource_id: Optional[str] = None,
        internal_message: Optional[str] = None,
    ):
        normalized_reason = reason.upper()
        if normalized_reason not in {"ALREADY_EXISTS", "ABORTED"}:
            normalized_reason = "ABORTED"

        details: Dict[str, Any] = {"reason": normalized_reason}
        if resource:
            details["resource"] = resource
        if resource_id:
            details["id"] = resource_id

        error_code = (
            ErrorCode.ALREADY_EXISTS
            if normalized_reason == "ALREADY_EXISTS"
            else ErrorCode.ABORTED
        )
        super().__init__(
            code=error_code,
            internal_message=internal_message or user_message,
            user_message=user_message,
            details=details,
        )


class RateLimitException(KnowhereException):
    """
    Rate limit exceeded. HTTP 429. RETRYABLE.

    4xx Error: Developer provides `user_message` that user sees directly.

    Default limits (from docs/limitation.md):
        - Free tier: 60 RPM
        - Pro tier: 120 RPM
        - Ultra tier: 300 RPM
        - Default retry_after: 15 seconds

    Details schema:
        {
            "reason": "RATE_LIMIT_EXCEEDED",
            "retry_after": <seconds>,
            "limit": <max_requests>,
            "period": "second" | "minute" | "hour" | "day"
        }
    """

    # Default values from docs/limitation.md
    DEFAULT_RETRY_AFTER = 15  # seconds
    DEFAULT_LIMIT = 60  # requests per minute (free tier)
    DEFAULT_PERIOD = "minute"

    def __init__(
        self,
        retry_after: int = DEFAULT_RETRY_AFTER,
        limit: int = DEFAULT_LIMIT,
        period: str = DEFAULT_PERIOD,
        user_message: str = "Rate limit exceeded. Please retry after {retry_after} seconds.",
        internal_message: Optional[str] = None,
    ):
        # Format user_message with retry_after if placeholder exists
        formatted_user_message = (
            user_message.format(retry_after=retry_after)
            if "{retry_after}" in user_message
            else user_message
        )

        details: Dict[str, Any] = {
            "reason": SubCode.RATE_LIMIT_EXCEEDED.value,
            "retry_after": retry_after,
            "limit": limit,
            "period": period,
        }

        super().__init__(
            code=ErrorCode.RESOURCE_EXHAUSTED,
            internal_message=internal_message
            or f"Rate limit exceeded: {limit} requests per {period}, retry_after={retry_after}s",
            user_message=formatted_user_message,
            details=details,
        )
        self.retry_after = retry_after
        self.limit = limit
        self.period = period


class QuotaExceededException(KnowhereException):
    """
    Quota exceeded. HTTP 429. NOT RETRYABLE.

    4xx Error: Developer provides `user_message` that user sees directly.

    Details schema:
        {"reason": "QUOTA_EXCEEDED", "quota_name": "...", "limit": <int>}
    """

    def __init__(
        self,
        quota_name: str,
        limit: int,
        user_message: str = "Quota exceeded",
        internal_message: Optional[str] = None,
    ):
        super().__init__(
            code=ErrorCode.RESOURCE_EXHAUSTED,
            internal_message=internal_message
            or f"Quota {quota_name} exceeded, limit={limit}",
            user_message=user_message,
            details={
                "reason": SubCode.QUOTA_EXCEEDED.value,
                "quota_name": quota_name,
                "limit": limit,
            },
        )


# ============================================================================
# Server Error Exceptions (5xx)
# ----------------------------------------------------------------------------
# For 5xx errors, developer provides `internal_message` for debugging.
# `user_message` auto-defaults to a safe generic message.
# Developer CAN override `user_message` with a custom safe message.
# ============================================================================


class UnavailableException(KnowhereException):
    """
    Service temporarily unavailable. HTTP 503. RETRYABLE.

    5xx Error: Auto-defaults to safe user_message.

    Details schema:
        {
            "retry_after": <seconds>,
            "limit": <max_requests> (optional),
            "period": "second" | "minute" | "hour" | "day" (optional)
        }
    """

    def __init__(
        self,
        internal_message: str,
        retry_after: int,
        limit: Optional[int] = None,
        period: Optional[str] = None,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        details: Dict[str, Any] = {"retry_after": retry_after}
        if limit is not None:
            details["limit"] = limit
        if period is not None:
            details["period"] = period

        super().__init__(
            code=ErrorCode.UNAVAILABLE,
            internal_message=internal_message,
            user_message=user_message,  # Defaults to generic 5xx message
            details=details,
            original_exception=original_exception,
        )
        self.retry_after = retry_after
        self.limit = limit
        self.period = period


class TimeoutException(KnowhereException):
    """
    Request timed out. HTTP 504. RETRYABLE.

    5xx Error: Auto-defaults to safe user_message.

    Details schema:
        {
            "retry_after": <seconds>,
            "limit": <max_requests> (optional),
            "period": "second" | "minute" | "hour" | "day" (optional)
        }
    """

    def __init__(
        self,
        internal_message: str,
        retry_after: int,
        limit: Optional[int] = None,
        period: Optional[str] = None,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        details: Dict[str, Any] = {"retry_after": retry_after}
        if limit is not None:
            details["limit"] = limit
        if period is not None:
            details["period"] = period

        super().__init__(
            code=ErrorCode.DEADLINE_EXCEEDED,
            internal_message=internal_message,
            user_message=user_message,  # Defaults to generic 5xx message
            details=details,
            original_exception=original_exception,
        )
        self.retry_after = retry_after
        self.limit = limit
        self.period = period


class UnknownException(KnowhereException):
    """
    Wrapper for non-KnowhereException errors. HTTP 500.
    Like a syntax error in the code.

    5xx Error: Auto-defaults to safe user_message.
    Internal details are logged but NEVER sent to client.

    Use this to wrap unexpected exceptions so they conform to the API schema.

    Details schema: {} (empty for security)
    """

    def __init__(
        self,
        original_exception: Exception,
        user_message: str = "An unexpected error occurred",
    ):
        super().__init__(
            code=ErrorCode.UNKNOWN,
            internal_message=f"{type(original_exception).__name__}: {str(original_exception)}",
            user_message=user_message,
            details={},  # Empty for security
            original_exception=original_exception,
        )


# ============================================================================
# Worker-Specific Exceptions (5xx)
# ----------------------------------------------------------------------------
# These are system errors that occur during async worker processing.
# Developer provides `internal_message`; `user_message` auto-defaults.
# ============================================================================


class FileSystemException(KnowhereException):
    """
    File system operation failed (read, write, create directory). HTTP 500.

    5xx Error: Auto-defaults to safe user_message.

    SECURITY: `path` is stored internally but NOT exposed in details.

    Details schema:
        {"operation": "read" | "write" | "create_directory" | "delete"}
    """

    def __init__(
        self,
        internal_message: str,
        operation: str,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        # SECURITY: Do NOT include path in details (internal info)
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,  # Defaults to generic 5xx message
            details={"operation": operation},
            original_exception=original_exception,
        )


class PDFParsingException(KnowhereException):
    """
    PDF parsing failed (encrypted, corrupted, layout issues).

    4xx Error: User's file is problematic; they need to fix it.
    Developer provides `user_message` that user sees directly.

    Details schema:
        {"file_type": "pdf", "reason": "..."}
    """

    def __init__(
        self,
        user_message: str,
        reason: str = "PARSING_FAILED",
        internal_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INVALID_ARGUMENT,
            internal_message=internal_message or user_message,
            user_message=user_message,
            details={"file_type": "pdf", "reason": reason},
            original_exception=original_exception,
        )


class DocxParsingException(KnowhereException):
    """
    DOCX parsing failed (structure issues).

    4xx Error: User's file is problematic; they need to fix it.
    Developer provides `user_message` that user sees directly.

    Details schema:
        {"file_type": "docx", "reason": "..."}
    """

    def __init__(
        self,
        user_message: str,
        reason: str = "PARSING_FAILED",
        internal_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INVALID_ARGUMENT,
            internal_message=internal_message or user_message,
            user_message=user_message,
            details={"file_type": "docx", "reason": reason},
            original_exception=original_exception,
        )


class TableParsingException(KnowhereException):
    """
    Table extraction failed.

    4xx Error: User's file is problematic; they need to fix it.
    Developer provides `user_message` that user sees directly.

    Details schema:
        {"file_type": "table", "reason": "..."}
    """

    def __init__(
        self,
        user_message: str,
        reason: str = "PARSING_FAILED",
        internal_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INVALID_ARGUMENT,
            internal_message=internal_message or user_message,
            user_message=user_message,
            details={"file_type": "table", "reason": reason},
            original_exception=original_exception,
        )


class ImageParsingException(KnowhereException):
    """
    Image processing/OCR failed.

    4xx Error: User's file is problematic; they need to fix it.
    Developer provides `user_message` that user sees directly.

    Details schema:
        {"file_type": "image", "reason": "..."}
    """

    def __init__(
        self,
        user_message: str,
        reason: str = "PARSING_FAILED",
        internal_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INVALID_ARGUMENT,
            internal_message=internal_message or user_message,
            user_message=user_message,
            details={"file_type": "image", "reason": reason},
            original_exception=original_exception,
        )


class TextParsingException(KnowhereException):
    """
    Text decoding or format failed.

    4xx Error: User's file is problematic; they need to fix it.
    Developer provides `user_message` that user sees directly.

    Details schema:
        {"file_type": "text", "reason": "..."}
    """

    def __init__(
        self,
        user_message: str,
        reason: str = "PARSING_FAILED",
        internal_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INVALID_ARGUMENT,
            internal_message=internal_message or user_message,
            user_message=user_message,
            details={"file_type": "text", "reason": reason},
            original_exception=original_exception,
        )


class LLMServiceException(KnowhereException):
    """
    LLM service call failed.

    5xx Error: Auto-defaults to safe user_message.

    Details schema:
        {"service": "...", "status_code": <int>}
    """

    def __init__(
        self,
        internal_message: str,
        provider: str = "llm",
        status_code: Optional[int] = None,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        details: Dict[str, Any] = {"service": provider}
        if status_code is not None:
            details["status_code"] = status_code
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,  # Defaults to generic 5xx message
            details=details,
            original_exception=original_exception,
        )


class StorageServiceException(KnowhereException):
    """
    Storage service (S3/MinIO) failed.

    5xx Error: Auto-defaults to safe user_message.

    Details schema:
        {"service": "storage", "operation": "..."}
    """

    def __init__(
        self,
        internal_message: str,
        operation: str = "unknown",
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,  # Defaults to generic 5xx message
            details={"service": "storage", "operation": operation},
            original_exception=original_exception,
        )


class LibreOfficeServiceException(KnowhereException):
    """
    LibreOffice document conversion failed.

    5xx Error: Auto-defaults to safe user_message.

    Details schema:
        {"service": "libreoffice", "operation": "...", "exit_code": <int>}
    """

    def __init__(
        self,
        internal_message: str,
        operation: str = "unknown",
        exit_code: Optional[int] = None,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        details: Dict[str, Any] = {
            "service": "libreoffice",
            "operation": operation,
        }
        if exit_code is not None:
            details["exit_code"] = exit_code
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,  # Defaults to generic 5xx message
            details=details,
            original_exception=original_exception,
        )


class RedisServiceException(KnowhereException):
    """
    Redis/Cache service failed.

    5xx Error: Auto-defaults to safe user_message.

    Details schema:
        {"service": "redis", "operation": "..."}
    """

    def __init__(
        self,
        internal_message: str,
        operation: str = "unknown",
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,  # Defaults to generic 5xx message
            details={"service": "redis", "operation": operation},
            original_exception=original_exception,
        )


class MinerUServiceException(KnowhereException):
    """
    Third-party PDF extraction service failed.

    5xx Error: Auto-defaults to safe user_message.

    Details schema:
        {"service": "document_processing", "status_code": <int>, "error_message": "..."}
    """

    def __init__(
        self,
        internal_message: str,
        status_code: Optional[int] = None,
        error_message: Optional[str] = None,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        details: Dict[str, Any] = {"service": "document_processing"}
        if status_code is not None:
            details["status_code"] = status_code
        # SECURITY: error_message might contain sensitive info, don't include in details
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,  # Defaults to generic 5xx message
            details=details,
            original_exception=original_exception,
        )


class WorkerHandlingException(KnowhereException):
    """
    Worker handling failed (internal logic error).

    5xx Error: Auto-defaults to safe user_message.

    Details schema: {} (empty for security)
    """

    def __init__(
        self,
        internal_message: str,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,  # Defaults to generic 5xx message
            details={},
            original_exception=original_exception,
        )


class SystemSettingMissingException(KnowhereException):
    """
    Required system setting is missing.

    5xx Error: Auto-defaults to safe user_message.

    Details schema: {} (empty for security)
    """

    def __init__(
        self,
        internal_message: str,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,  # Defaults to generic 5xx message
            details={},
            original_exception=original_exception,
        )


class SystemSettingInvalidException(KnowhereException):
    """
    System setting has an invalid value.

    5xx Error: Auto-defaults to safe user_message.

    Details schema: {} (empty for security)
    """

    def __init__(
        self,
        internal_message: str,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,  # Defaults to generic 5xx message
            details={},
            original_exception=original_exception,
        )


class DependencyMissingException(KnowhereException):
    """
    Required Python dependency is not installed.

    5xx Error: Auto-defaults to safe user_message.

    Details schema: {} (empty for security)
    """

    def __init__(
        self,
        internal_message: str,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,  # Defaults to generic 5xx message
            details={},
            original_exception=original_exception,
        )


class StripeServiceException(KnowhereException):
    """
    Stripe payment service operations failed.

    5xx Error: Auto-defaults to safe user_message.
    """

    def __init__(
        self,
        internal_message: str,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,
            details={"service": "stripe"},
            original_exception=original_exception,
        )


class ConcurrencyControlException(KnowhereException):
    """
    Concurrency control (locks, state machine) operations failed.

    5xx Error: Auto-defaults to safe user_message.
    """

    def __init__(
        self,
        internal_message: str,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,
            details={"component": "concurrency_control"},
            original_exception=original_exception,
        )


class APIKeyOperationException(KnowhereException):
    """
    API Key management operations failed.

    5xx Error: Auto-defaults to safe user_message.
    """

    def __init__(
        self,
        internal_message: str,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,
            details={"component": "api_key_management"},
            original_exception=original_exception,
        )


class JobOperationException(KnowhereException):
    """
    Job management operations failed.

    5xx Error: Auto-defaults to safe user_message.
    """

    def __init__(
        self,
        internal_message: str,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,
            details={"component": "job_management"},
            original_exception=original_exception,
        )


class EmailServiceException(KnowhereException):
    """
    Email service operations failed.

    5xx Error: Auto-defaults to safe user_message.
    """

    def __init__(
        self,
        internal_message: str,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,
            details={"service": "email"},
            original_exception=original_exception,
        )


class WebhookServiceException(KnowhereException):
    """
    Webhook service operations failed.

    5xx Error: Auto-defaults to safe user_message.
    """

    def __init__(
        self,
        internal_message: str,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,
            details={"service": "webhook"},
            original_exception=original_exception,
        )


class QStashServiceException(KnowhereException):
    """
    QStash service operations failed.

    5xx Error: Auto-defaults to safe user_message.
    """

    def __init__(
        self,
        internal_message: str,
        operation: str = "publish_webhook",
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,
            details={"service": "qstash", "operation": operation},
            original_exception=original_exception,
        )


class UndefinedSubscriptionPlanException(KnowhereException):
    """
    Subscription plan is undefined.

    5xx Error: Auto-defaults to safe user_message.
    """

    def __init__(
        self,
        internal_message: str,
        user_message: Optional[str] = None,
        original_exception: Optional[Exception] = None,
    ):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            internal_message=internal_message,
            user_message=user_message,
            details={"component": "subscription"},
            original_exception=original_exception,
        )
