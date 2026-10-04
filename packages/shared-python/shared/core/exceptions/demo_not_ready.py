"""Public readiness failure for the shared demo corpus."""

from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.core.response.ErrorCode import ErrorCode


class DemoNotReadyException(KnowhereException):
    def __init__(self, *, is_explicit_source: bool) -> None:
        super().__init__(code=ErrorCode.DEMO_NOT_READY, internal_message="Demo source has no complete published revision.", user_message="Demo documents are preparing. Retry after publication completes.", http_status_code=409 if is_explicit_source else 503, details={"retry_after": 30})
