"""Shared token-ledger helpers for debug parse scripts."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

_NUMERIC_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "calls")


def empty_token_usage() -> dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
        "by_model": {},
        "by_task": {},
    }


def token_usage_delta(prev: dict[str, Any], cur: dict[str, Any]) -> dict[str, Any]:
    numeric = _NUMERIC_USAGE_FIELDS

    def _sub(left: dict[str, Any], right: dict[str, Any]) -> dict[str, int]:
        return {
            field: int(right.get(field, 0)) - int(left.get(field, 0)) for field in numeric
        }

    def _bucket(prev_bucket: dict[str, Any], cur_bucket: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for key in set(prev_bucket) | set(cur_bucket):
            prev_item = prev_bucket.get(key, {})
            cur_item = cur_bucket.get(key, {})
            if not isinstance(prev_item, dict) or not isinstance(cur_item, dict):
                continue
            entry = {
                field: value for field, value in _sub(prev_item, cur_item).items() if value
            }
            prev_models = prev_item.get("models", {})
            cur_models = cur_item.get("models", {})
            if prev_models or cur_models:
                models = _bucket(prev_models, cur_models)
                if models:
                    entry["models"] = models
            if entry:
                merged[key] = entry
        return merged

    delta = _sub(prev, cur)
    for bucket_key in ("by_model", "by_task"):
        bucket = _bucket(prev.get(bucket_key, {}), cur.get(bucket_key, {}))
        if bucket:
            delta[bucket_key] = bucket
    return delta


def merge_token_usage(destination: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict):
            child = destination.setdefault(str(key), {})
            if isinstance(child, dict):
                merge_token_usage(child, value)
        elif isinstance(value, int | float) and not isinstance(value, bool):
            destination[str(key)] = destination.get(str(key), 0) + value


def load_stage_ledger(path: Path, *, version: str = "1.0") -> dict[str, Any]:
    if not path.exists():
        return {"version": version, "stages": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"version": version, "stages": {}}
    data.setdefault("version", version)
    data.setdefault("stages", {})
    return data


def write_stage_ledger(path: Path, ledger: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def record_stage_delta(
    ledger: dict[str, Any],
    *,
    stage: str,
    stage_keys: tuple[str, ...],
    prev: dict[str, Any],
    current: dict[str, Any],
    out_path: Path,
) -> dict[str, Any]:
    """Store per-stage delta and drop stale downstream stage keys."""
    stages = ledger.setdefault("stages", {})
    if not isinstance(stages, dict):
        stages = {}
        ledger["stages"] = stages
    stage_index = stage_keys.index(stage)
    for stale in stage_keys[stage_index + 1 :]:
        stages.pop(stale, None)
    stages[stage] = {"token_usage": token_usage_delta(prev, current)}
    write_stage_ledger(out_path, ledger)
    return deepcopy(current)


def aggregate_stage_deltas(
    ledger: dict[str, Any],
    stage_keys: tuple[str, ...],
    *,
    remainder: dict[str, Any] | None = None,
) -> dict[str, Any]:
    usage = empty_token_usage()
    stages = ledger.get("stages")
    if isinstance(stages, dict):
        for stage in stage_keys:
            row = stages.get(stage)
            if not isinstance(row, dict):
                continue
            raw_usage = row.get("token_usage")
            if isinstance(raw_usage, dict):
                merge_token_usage(usage, raw_usage)
    if remainder:
        merge_token_usage(usage, remainder)
    return usage
