"""Request-scoped decoded serving-manifest reuse."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_CACHE_INFO_KEY = "retrieval_serving_manifest_payloads"


def manifest_revision_key(revisions: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Return a stable key for one immutable revision pin set."""
    return tuple(sorted((str(document_id), str(job_result_id)) for document_id, job_result_id in revisions.items()))


def get_cached_manifest_payloads(
    db: object,
    *,
    revisions: Mapping[str, str],
) -> dict[tuple[str, str], dict[str, Any]] | None:
    """Return decoded manifests cached on this request's SQLAlchemy session."""
    info = getattr(db, "info", None)
    if not isinstance(info, dict):
        return None
    batches = info.get(_CACHE_INFO_KEY)
    if not isinstance(batches, dict):
        return None
    payloads = batches.get(manifest_revision_key(revisions))
    if not isinstance(payloads, dict):
        return None
    return payloads


def cache_manifest_payloads(
    db: object,
    *,
    revisions: Mapping[str, str],
    payloads: dict[tuple[str, str], dict[str, Any]],
) -> None:
    """Store one complete decoded manifest batch for this request only."""
    info = getattr(db, "info", None)
    if not isinstance(info, dict):
        return
    batches = info.setdefault(_CACHE_INFO_KEY, {})
    if isinstance(batches, dict):
        batches[manifest_revision_key(revisions)] = payloads
