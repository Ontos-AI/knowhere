"""Classify internal input failures and later explicit retry eligibility."""

import httpx


class LexicalIndexError(RuntimeError):
    INPUT_TOO_LARGE: str = "INDEXING_INPUT_TOO_LARGE"

    @staticmethod
    def isUnavailableStatus(status: int) -> bool:
        return status in (401, 403, 404, 408, 409, 429, 500, 502, 503, 504)

    def __init__(self, message: str, *, isRetryable: bool = False,
                 hasUnknownOutcome: bool = False, isIndexClosed: bool = False,
                 isTimeout: bool = False, isSizeLimit: bool = False) -> None:
        super().__init__(message)
        self.isRetryable: bool = isRetryable
        self.hasUnknownOutcome: bool = hasUnknownOutcome
        self.isIndexClosed: bool = isIndexClosed
        self.isTimeout: bool = isTimeout
        self.isSizeLimit: bool = isSizeLimit

    def getIndexingFailureCode(self) -> str:
        if self.isTimeout:
            return "DEADLINE_EXCEEDED"
        if self.isRetryable:
            return "UNAVAILABLE"
        return self.INPUT_TOO_LARGE if self.isSizeLimit else "INTERNAL_ERROR"

    @classmethod
    def fromHttpResponse(cls, response: httpx.Response) -> "LexicalIndexError":
        isClosed: bool = False
        try:
            payload: object = response.json()
            details: object = payload.get("error") if isinstance(payload, dict) else None
            isClosed = isinstance(details, dict) and details.get("type") == "index_closed_exception"
        except ValueError:
            pass
        return cls(f"Native lexical operation failed with HTTP {response.status_code}",
            isRetryable=isClosed or cls.isUnavailableStatus(response.status_code),
            isIndexClosed=isClosed)
