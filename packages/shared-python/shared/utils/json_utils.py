"""JSON-serialization helpers."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Mapping, MutableSet


def make_json_safe(
    value: Any, *, max_preview_rows: int = 5, _visited: MutableSet[int] | None = None
) -> Any:
    """
    Convert a complex object into a JSON-safe structure.

    Args:
        value: Object to serialize.
        max_preview_rows: Maximum preview rows for DataFrame-like objects.
        _visited: Internal visited-object set used to avoid cycles.

    Returns:
        JSON-serializable data.
    """
    if _visited is None:
        _visited = set()

    basic_types = (str, int, float, bool, type(None))
    if isinstance(value, basic_types):
        return value

    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()

    if isinstance(value, Path):
        return str(value)

    # Handle UUID-like types, including asyncpg UUID wrappers.
    if hasattr(value, "__class__") and "UUID" in value.__class__.__name__:
        return str(value)

    obj_id = id(value)
    if obj_id in _visited:
        return "<recursive_reference>"
    _visited.add(obj_id)

    try:
        import numpy as np  # type: ignore
    except Exception:  # pragma: no cover
        np = None  # type: ignore

    if np is not None:
        if isinstance(value, np.generic):
            _visited.discard(obj_id)
            return value.item()
        if isinstance(value, np.ndarray):
            _visited.discard(obj_id)
            return {
                "__type__": "ndarray",
                "shape": value.shape,
                "dtype": str(value.dtype),
            }

    try:
        import pandas as pd  # type: ignore
    except Exception:  # pragma: no cover
        pd = None  # type: ignore

    if pd is not None:
        if isinstance(value, pd.DataFrame):
            preview = value.head(max_preview_rows).to_dict(orient="records")
            _visited.discard(obj_id)
            return {
                "__type__": "DataFrame",
                "rows": int(len(value)),
                "columns": list(map(str, value.columns)),
                "preview": preview,
            }
        if isinstance(value, pd.Series):
            preview = value.head(max_preview_rows).to_dict()
            _visited.discard(obj_id)
            return {
                "__type__": "Series",
                "length": int(len(value)),
                "preview": preview,
            }

    if isinstance(value, Mapping):
        serialized = {
            str(key): make_json_safe(
                val, max_preview_rows=max_preview_rows, _visited=_visited
            )
            for key, val in value.items()
        }
        _visited.discard(obj_id)
        return serialized

    if isinstance(value, (list, tuple, set, frozenset)):
        serialized_seq = [
            make_json_safe(item, max_preview_rows=max_preview_rows, _visited=_visited)
            for item in value
        ]
        _visited.discard(obj_id)
        return serialized_seq

    if hasattr(value, "__dict__"):
        serialized = {
            str(key): make_json_safe(
                val, max_preview_rows=max_preview_rows, _visited=_visited
            )
            for key, val in vars(value).items()
        }
        serialized["__type__"] = value.__class__.__name__
        _visited.discard(obj_id)
        return serialized

    _visited.discard(obj_id)
    return str(value)
