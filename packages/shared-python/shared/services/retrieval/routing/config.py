from __future__ import annotations

import os

from shared.services.retrieval.agent_explore.config import AGENT_EXPLORE_MODEL

QUERY_ROUTER_ENABLED_ENV = "RETRIEVAL_QUERY_ROUTER_ENABLED"
QUERY_ROUTER_MODEL_ENV = "RETRIEVAL_QUERY_ROUTER_MODEL"

QUERY_ROUTER_TIMEOUT_SECONDS = 10
QUERY_ROUTER_MAX_TOKENS = 256


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


QUERY_ROUTER_ENABLED = _env_flag(QUERY_ROUTER_ENABLED_ENV, True)
QUERY_ROUTER_MODEL = (
    os.environ.get(QUERY_ROUTER_MODEL_ENV, "").strip() or AGENT_EXPLORE_MODEL
)
