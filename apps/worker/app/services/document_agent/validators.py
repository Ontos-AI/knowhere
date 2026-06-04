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
    min_pages: int,
    max_pages: int,
) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    if not plan.shards:
        errors.append("shard_plan has no shards")
        return ValidationReport(valid=False, errors=errors, warnings=warnings)
    sorted_shards = sorted(plan.shards, key=lambda item: item.shard_index)
    expected_start = 1
    for idx, shard in enumerate(sorted_shards):
        is_last = idx == len(sorted_shards) - 1
        if shard.page_start != expected_start:
            errors.append(
                f"shard {shard.shard_index} starts at {shard.page_start}, expected {expected_start}"
            )
        if shard.page_end < shard.page_start:
            errors.append(f"shard {shard.shard_index} has invalid range")
        if shard.page_offset != shard.page_start - 1:
            errors.append(f"shard {shard.shard_index} page_offset mismatch")
        length = shard.page_end - shard.page_start + 1
        if plan.enabled and length > max_pages:
            errors.append(f"shard {shard.shard_index} exceeds max_pages={max_pages}")
        if plan.enabled and length < min_pages:
            if is_last:
                warnings.append(
                    f"shard {shard.shard_index} (final) shorter than min_pages={min_pages} "
                    f"({length} pages)"
                )
            else:
                errors.append(f"shard {shard.shard_index} shorter than min_pages={min_pages}")
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
                confidence=1.0,
            )
        ],
    )


def validate_anatomy_map(
    anatomy: PageAnatomyMap,
    *,
    min_pages: int,
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
    toc_pages = set(anatomy.toc_result.toc_pages)
    for candidate in anatomy.h1_result.h1_candidates:
        if candidate.page in toc_pages:
            errors.append(f"h1 candidate points to toc page {candidate.page}")
        if candidate.page < 1 or candidate.page > page_count:
            errors.append(f"h1 candidate page {candidate.page} out of range")
    shard_report = validate_shard_plan(
        anatomy.shard_plan,
        page_count=page_count,
        min_pages=min_pages,
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
