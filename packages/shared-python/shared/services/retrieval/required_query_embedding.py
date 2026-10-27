"""A required retrieval channel failed; agent tool recovery cannot mask it."""

from shared.core.exceptions.domain_exceptions import LLMServiceException


class RequiredQueryEmbeddingException(LLMServiceException):
    """Abort the retrieval request instead of returning incomplete evidence."""
