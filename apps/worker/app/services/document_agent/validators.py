"""Validation and repair for page anatomy outputs."""

from __future__ import annotations

from app.services.document_agent.manifest import (
    PageAnatomyMap,
    Shard,
    ShardPlan,
    ValidationReport,
)


def validate_shard_plan(
    plan: ShardPlan,
    *,
    page_count: int,
    max_pages: int,
) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    if not plan.shards:
        errors.append("shard_plan has no shards")
        return ValidationReport(valid=False, errors=errors, warnings=warnings)
    sorted_shards = sorted(plan.shards, key=lambda item: item.shard_index)
    expected_start = 1
    for shard in sorted_shards:
        if shard.page_start != expected_start:
            errors.append(
                f"shard {shard.shard_index} starts at {shard.page_start}, expected {expected_start}"
            )
        if shard.page_end < shard.page_start:
            errors.append(f"shard {shard.shard_index} has invalid range")
        if shard.page_offset != shard.page_start - 1:
            errors.append(f"shard {shard.shard_index} page_offset mismatch")
        length = shard.page_end - shard.page_start + 1
        if length > max_pages:
            errors.append(f"shard {shard.shard_index} exceeds max_pages={max_pages}")
        expected_start = shard.page_end + 1
    if expected_start != page_count + 1:
        errors.append("shard_plan does not cover full document")
    return ValidationReport(valid=not errors, errors=errors, warnings=warnings)


def single_shard_plan(page_count: int) -> ShardPlan:
    return ShardPlan(
        enabled=False,
        reason="not_needed",
        shards=[
            Shard(
                shard_index=0,
                page_start=1,
                page_end=max(page_count, 1),
                page_offset=0,
                anchor_type="forced_max_size",
                anchor_evidence="document within shard threshold",
            )
        ],
    )


def validate_anatomy_map(
    anatomy: PageAnatomyMap,
    *,
    max_pages: int,
) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    page_count = anatomy.page_count
    feature_pages = {feature.page for feature in anatomy.page_features}
    label_pages = {label.page for label in anatomy.page_labels}
    expected_pages = set(range(1, page_count + 1))
    if feature_pages != expected_pages:
        errors.append("page_features do not cover every page")
    if label_pages != expected_pages:
        errors.append("page_labels do not cover every page")
    shard_report = validate_shard_plan(
        anatomy.shard_plan,
        page_count=page_count,
        max_pages=max_pages,
    )
    errors.extend(shard_report.errors)
    warnings.extend(shard_report.warnings)
    if anatomy.shard_plan.enabled:
        forced_count = sum(
            1
            for shard in anatomy.shard_plan.shards
            if shard.anchor_type == "forced_max_size"
        )
        if forced_count == len(anatomy.shard_plan.shards):
            warnings.append("all shards are based on forced max-size boundaries")
    return ValidationReport(valid=not errors, errors=errors, warnings=warnings)
