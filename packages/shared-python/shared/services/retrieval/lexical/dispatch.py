"""Retain a harmless compatibility entry for obsolete indexing dispatch calls."""

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker


def dispatchIndexing(jobId: str, *, sessionFactory: sessionmaker[Session] | None = None) -> bool:
    # Primary indexing belongs to its original parse task or retry request.
    # Delayed imports/callers cannot create a separate task or extend a deadline.
    logger.warning("Ignored obsolete primary indexing dispatch: job_id={}", jobId)
    return False
