"""Per-document navigation state for the collector runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shared.services.retrieval.agentic.navigation.path_ledger import PathLedger


@dataclass
class NavigationState:
    """Mutable state for one document navigation loop."""

    document_id: str
    document_name: str
    job_result_id: str
    current_scope: str | None = None
    expanded_scopes: set[str] = field(default_factory=set)
    rejected_paths: set[str] = field(default_factory=set)
    rejected_collect_paths: set[str] = field(default_factory=set)
    collected_paths: list[dict[str, Any]] = field(default_factory=list)
    nav_trace: list[dict[str, Any]] = field(default_factory=list)
    tool_history: list[dict[str, Any]] = field(default_factory=list)
    blocked_asset_searches: set[str] = field(default_factory=set)
    step_count: int = 0

    def snapshot_delta(
        self,
        *,
        before_scope: str | None,
        expanded_before: set[str],
        rejected_before: set[str],
        rejected_collect_before: set[str],
        collected_before_count: int,
    ) -> dict[str, Any]:
        return {
            "current_scope_before": before_scope or "root",
            "current_scope_after": self.current_scope or "root",
            "expanded_added": sorted(self.expanded_scopes - expanded_before),
            "rejected_added": sorted(self.rejected_paths - rejected_before),
            "rejected_collect_added": sorted(
                self.rejected_collect_paths - rejected_collect_before
            ),
            "collected_added": [
                item.get("path", "")
                for item in self.collected_paths[collected_before_count:]
                if item.get("path")
            ],
        }

    def add_collected(
        self,
        item: dict[str, Any],
        *,
        step: int,
        scope_context: str | None,
    ) -> dict[str, Any]:
        enriched = dict(item)
        enriched["collected_at_step"] = step
        enriched["scope_context"] = scope_context or "root"
        self.collected_paths.append(enriched)
        return enriched

    def mark_expanded(self, path: str | None) -> None:
        normalized = PathLedger.normalize(path)
        if normalized:
            self.expanded_scopes.add(normalized)

    def mark_rejected_collect(self, path: str | None) -> None:
        normalized = PathLedger.normalize(path)
        if normalized:
            self.rejected_paths.add(normalized)
            self.rejected_collect_paths.add(normalized)

    def mark_rejected_if_unproductive(self, path: str | None) -> None:
        normalized = PathLedger.normalize(path)
        if not normalized:
            return
        has_full_collect = any(
            item.get("hydrate_mode") != "outline"
            and PathLedger.is_same_or_descendant(item.get("path"), normalized)
            for item in self.collected_paths
        )
        if not has_full_collect:
            self.rejected_paths.add(normalized)

    def blocked_asset_types_for_scope(self, scope: str | None) -> set[str]:
        prefix = f"{PathLedger.normalize(scope) or 'root'}:"
        return {
            key.split(":", 1)[1]
            for key in self.blocked_asset_searches
            if key.startswith(prefix)
        }

    def block_asset_search(self, scope: str | None, asset_type: str) -> None:
        normalized_scope = PathLedger.normalize(scope) or "root"
        normalized_type = asset_type.strip().lower()
        if normalized_type:
            self.blocked_asset_searches.add(f"{normalized_scope}:{normalized_type}")
