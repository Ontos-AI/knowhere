"""Resolve and announce the process-wide Publication strategy.

Worker and API processes announce the selected strategy once at startup. The
resolver deliberately accepts no override arguments: the temporary strategy is
global, and request-, namespace-, or percentage-based routing is out of scope.
"""

from __future__ import annotations

from loguru import logger

from shared.core.config import settings
from shared.core.config.publication import (
    DEFAULT_PUBLICATION_STRATEGY,
    PUBLICATION_STRATEGIES,
    PUBLICATION_STRATEGY_ENV_VAR,
    PublicationStrategy,
    PublicationStrategyError,
    normalize_publication_strategy,
)

__all__ = [
    "DEFAULT_PUBLICATION_STRATEGY",
    "PUBLICATION_STRATEGIES",
    "PUBLICATION_STRATEGY_ENV_VAR",
    "PublicationStrategy",
    "PublicationStrategyAnnouncer",
    "PublicationStrategyError",
    "announce_publication_strategy",
    "normalize_publication_strategy",
    "publication_strategy_announcer",
    "resolve_publication_strategy",
]


def resolve_publication_strategy() -> PublicationStrategy:
    """Return the process-wide strategy; nothing may override it per request."""
    return normalize_publication_strategy(settings.KNOWHERE_PUBLICATION_STRATEGY)


class PublicationStrategyAnnouncer:
    """Announce the selected strategy exactly once per process."""

    def __init__(self) -> None:
        self._has_announced: bool = False

    def announce(self, *, service_name: str) -> PublicationStrategy:
        """Log the selected strategy once and return it."""
        strategy = resolve_publication_strategy()
        if not self._has_announced:
            self._has_announced = True
            logger.info(
                f"{service_name} publication strategy: {strategy} "
                f"({PUBLICATION_STRATEGY_ENV_VAR})"
            )
        return strategy


publication_strategy_announcer = PublicationStrategyAnnouncer()


def announce_publication_strategy(service_name: str) -> PublicationStrategy:
    """Announce the strategy for a service through the process singleton."""
    return publication_strategy_announcer.announce(service_name=service_name)
