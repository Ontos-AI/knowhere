from __future__ import annotations

DEFAULT_RETRIEVAL_NAMESPACE = "default"


def normalize_retrieval_namespace(namespace: str | None) -> str:
    """Return the canonical namespace value used by jobs, retrieval, and caches."""
    normalized = str(namespace or "").strip()
    return normalized or DEFAULT_RETRIEVAL_NAMESPACE
