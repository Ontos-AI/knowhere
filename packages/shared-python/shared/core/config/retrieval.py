"""Retrieval configuration settings"""

from pydantic import Field
from pydantic_settings import BaseSettings


class RetrievalConfig(BaseSettings):
    """Retrieval configuration settings"""

    RETRIEVAL_POSTGRES_FTS_CANDIDATE_LIMIT: int = Field(
        default=2000,
        ge=1,
        description=(
            "Maximum rows the Postgres FTS prefilter returns per BM25 channel "
            "before Python BM25 reranking. Larger values trade memory for recall."
        ),
    )
