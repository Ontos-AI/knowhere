"""Close least-recently-used indexes when too many are open.

Opening an index requests one turnover (``LexicalMaintenanceSignals.requestTurnover``),
and the hourly ``maintainLexical`` sweep runs it as a safety net. At the hard
open-index limit, index admission still evicts inline.
"""

from loguru import logger

from shared.core.celery_app import celery_app
from shared.core.config import settings
from shared.core.database_sync import get_sync_session_factory
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.maintenance_signals import TURNOVER_TASK
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope


def turnOverIndexes() -> int:
    """Run one bounded high-to-low turnover; returns the number of closed indexes."""
    if settings.KNOWHERE_LEXICAL_BACKEND != "opensearch":
        return 0
    native: OpenSearchLexicalIndex = OpenSearchLexicalIndex(
        endpoint=settings.KNOWHERE_OPENSEARCH_URL,
        scope=LexicalScope(ownerId="lifecycle", namespace="inventory"),
        indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX,
    )
    try:
        return LexicalCoordination(get_sync_session_factory()).trimIdle(native)
    except Exception as error:
        logger.error(
            "Lexical index turnover needs attention: error_type={}",
            type(error).__name__,
        )
        return 0
    finally:
        native.close()


@celery_app.task(name=TURNOVER_TASK)
def turnOverLexicalIndexes() -> int:
    return turnOverIndexes()
