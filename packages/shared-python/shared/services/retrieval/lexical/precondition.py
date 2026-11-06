"""A parsed revision cannot currently accept an indexing-only retry."""

from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.core.response.ErrorCode import ErrorCode


class IndexingPreconditionError(KnowhereException):
    def __init__(self, message: str) -> None:
        super().__init__(code=ErrorCode.FAILED_PRECONDITION,
                         internal_message=message, user_message=message)
