from __future__ import annotations

DEFAULT_RETRIEVAL_NAMESPACE = "default"

# Backwards-compatible private alias.
_DEFAULT_RETRIEVAL_NAMESPACE = DEFAULT_RETRIEVAL_NAMESPACE


def normalize_retrieval_namespace(namespace: str | None) -> str:
    """Return the canonical namespace value used by jobs, retrieval, and caches."""
    normalized = str(namespace or "").strip()
    return normalized or DEFAULT_RETRIEVAL_NAMESPACE
