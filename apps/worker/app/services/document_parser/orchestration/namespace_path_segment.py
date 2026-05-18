from __future__ import annotations

from app.services.common.file_utils import path_handle

_DEFAULT_NAMESPACE_SEGMENT = "default"


def build_namespace_path_segment(namespace: str | None) -> str:
    """Map a retrieval namespace to one safe parser path segment."""
    raw_namespace = (namespace or _DEFAULT_NAMESPACE_SEGMENT).strip()
    sanitized_namespace = path_handle(raw_namespace, mode="clean_single")
    if not isinstance(sanitized_namespace, str):
        return _DEFAULT_NAMESPACE_SEGMENT

    namespace_segment = sanitized_namespace.strip()
    if namespace_segment in {"", ".", ".."}:
        return _DEFAULT_NAMESPACE_SEGMENT
    return namespace_segment
