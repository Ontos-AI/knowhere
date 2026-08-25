"""Token cost estimation for AI model usage snapshots."""

from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from datetime import date
from typing import Any

from loguru import logger

from shared.core.config import settings

DEFAULT_TOKEN_PRICING_TABLE: dict[str, dict[str, Any]] = {
    # DeepSeek official (peak/off-peak since 2026-08-16 16:00 UTC).
    # Defaults use OFF-PEAK; peak rates are 2x and run 01:00-04:00 & 06:00-10:00 UTC,
    # Monday through Friday only — the whole weekend is off-peak.
    "deepseek-v4-flash": {
        "currency": "USD",
        "unit": "per_1m_tokens",
        "input_per_1m": 0.22,
        "cached_input_per_1m": 0.007,
        "output_per_1m": 0.66,
        "peak_input_per_1m": 0.44,
        "peak_cached_input_per_1m": 0.014,
        "peak_output_per_1m": 1.32,
        "source": (
            "DeepSeek official pricing "
            "(https://api-docs.deepseek.com/quick_start/pricing); "
            "defaults are off-peak cache-miss rates"
        ),
        "effective_date": "2026-08-17",
    },
    "deepseek-chat": {
        "alias_of": "deepseek-v4-flash",
    },
    "deepseek-reasoner": {
        "alias_of": "deepseek-v4-flash",
    },
    # China mainland DashScope (ALI_URL=dashscope.aliyuncs.com), <=256K tier.
    # Official CNY is ¥1.2 / ¥7.2 per 1M; Bailian USD display is $0.165 / $0.99.
    "qwen3.6-flash": {
        "currency": "USD",
        "unit": "per_1m_tokens",
        "input_per_1m": 0.165,
        "output_per_1m": 0.99,
        "source": (
            "Alibaba Bailian China mainland qwen3.6-flash <=256K "
            "(¥1.2/¥7.2 per 1M; USD list $0.165/$0.99). "
            "International endpoint is $0.25/$1.50"
        ),
        "effective_date": "2026-08-17",
    },
}


def build_token_cost_estimate(token_usage: dict[str, Any] | None) -> dict[str, Any]:
    """Build a cost estimate from token usage grouped by model and task."""
    if not isinstance(token_usage, dict):
        return {}

    pricing_table = load_token_pricing_table()
    by_model_usage = token_usage.get("by_model")
    if not isinstance(by_model_usage, dict):
        by_model_usage = {}

    by_model_cost: dict[str, Any] = {}
    missing_models: list[str] = []
    total_input_cost = 0.0
    total_output_cost = 0.0

    for model_name, usage in by_model_usage.items():
        if not isinstance(usage, dict):
            continue
        model_cost = _estimate_usage_cost(
            model_name=str(model_name),
            usage=usage,
            pricing_table=pricing_table,
        )
        by_model_cost[str(model_name)] = model_cost
        if model_cost.get("pricing_missing"):
            missing_models.append(str(model_name))
            continue
        total_input_cost += float(model_cost["input_cost"])
        total_output_cost += float(model_cost["output_cost"])

    by_task_cost = _estimate_task_costs(
        token_usage.get("by_task"),
        pricing_table=pricing_table,
    )

    total_cost = total_input_cost + total_output_cost
    return {
        "currency": "USD",
        "pricing_version": _pricing_version(pricing_table),
        "total_input_cost": _round_cost(total_input_cost),
        "total_output_cost": _round_cost(total_output_cost),
        "total_cost": _round_cost(total_cost),
        "by_model": by_model_cost,
        "by_task": by_task_cost,
        "missing_pricing_models": sorted(set(missing_models)),
    }


def load_token_pricing_table() -> dict[str, dict[str, Any]]:
    pricing_table = deepcopy(DEFAULT_TOKEN_PRICING_TABLE)
    override_raw = getattr(settings, "TOKEN_PRICING_TABLE_JSON", "")
    if not override_raw:
        return pricing_table

    try:
        override = json.loads(override_raw)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid TOKEN_PRICING_TABLE_JSON, using defaults: {}", exc)
        return pricing_table

    if not isinstance(override, dict):
        logger.warning("TOKEN_PRICING_TABLE_JSON must be a JSON object, using defaults")
        return pricing_table

    for model_name, entry in override.items():
        if isinstance(entry, dict):
            pricing_table[str(model_name)] = dict(entry)
    return pricing_table


def _estimate_task_costs(
    by_task_usage: Any,
    *,
    pricing_table: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(by_task_usage, dict):
        return {}

    by_task_cost: dict[str, Any] = {}
    for task_name, task_usage in by_task_usage.items():
        if not isinstance(task_usage, dict):
            continue
        models = task_usage.get("models")
        if not isinstance(models, dict):
            by_task_cost[str(task_name)] = _estimate_usage_cost(
                model_name="unknown",
                usage=task_usage,
                pricing_table=pricing_table,
            )
            continue

        task_input_cost = 0.0
        task_output_cost = 0.0
        model_costs: dict[str, Any] = {}
        missing_models: list[str] = []
        for model_name, model_usage in models.items():
            if not isinstance(model_usage, dict):
                continue
            model_cost = _estimate_usage_cost(
                model_name=str(model_name),
                usage=model_usage,
                pricing_table=pricing_table,
            )
            model_costs[str(model_name)] = model_cost
            if model_cost.get("pricing_missing"):
                missing_models.append(str(model_name))
                continue
            task_input_cost += float(model_cost["input_cost"])
            task_output_cost += float(model_cost["output_cost"])

        by_task_cost[str(task_name)] = {
            "input_cost": _round_cost(task_input_cost),
            "output_cost": _round_cost(task_output_cost),
            "total_cost": _round_cost(task_input_cost + task_output_cost),
            "models": model_costs,
            "missing_pricing_models": sorted(set(missing_models)),
        }
    return by_task_cost


def _estimate_usage_cost(
    *,
    model_name: str,
    usage: dict[str, Any],
    pricing_table: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    pricing = _resolve_pricing(model_name, pricing_table)
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    calls = int(usage.get("calls") or 0)

    if pricing is None:
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "calls": calls,
            "input_cost": 0.0,
            "output_cost": 0.0,
            "total_cost": 0.0,
            "pricing_missing": True,
        }

    input_rate = float(pricing.get("input_per_1m") or 0)
    output_rate = float(pricing.get("output_per_1m") or 0)
    input_cost = input_tokens / 1_000_000 * input_rate
    output_cost = output_tokens / 1_000_000 * output_rate
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "calls": calls,
        "input_rate_per_1m": input_rate,
        "output_rate_per_1m": output_rate,
        "input_cost": _round_cost(input_cost),
        "output_cost": _round_cost(output_cost),
        "total_cost": _round_cost(input_cost + output_cost),
        "pricing": _public_pricing_entry(pricing),
    }


def _resolve_pricing(
    model_name: str,
    pricing_table: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    seen: set[str] = set()
    current = model_name
    while current and current not in seen:
        seen.add(current)
        entry = pricing_table.get(current)
        if not isinstance(entry, dict):
            return None
        alias = entry.get("alias_of")
        if alias:
            current = str(alias)
            continue
        return entry
    return None


def _public_pricing_entry(pricing: dict[str, Any]) -> dict[str, Any]:
    return {
        key: pricing[key]
        for key in (
            "currency",
            "unit",
            "input_per_1m",
            "output_per_1m",
            "source",
            "effective_date",
        )
        if key in pricing
    }


def _pricing_version(pricing_table: dict[str, dict[str, Any]]) -> str:
    version = {
        str(model): {
            key: entry.get(key)
            for key in ("input_per_1m", "output_per_1m", "effective_date", "alias_of")
            if key in entry
        }
        for model, entry in pricing_table.items()
        if isinstance(entry, dict)
    }
    encoded = json.dumps(version, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{date.today().isoformat()}:{digest}"


def _round_cost(value: float) -> float:
    return round(float(value), 8)
