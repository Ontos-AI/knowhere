"""Per-document navigation state for the collector runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from shared.services.retrieval.agentic.navigation.path_ledger import PathLedger

RejectReason = Literal["tool_adjudicated", "navigational_abandon"]

# Strength ordering: a stronger reason overrides a weaker one so we keep the
# most informative record for a given path.
_REASON_STRENGTH: dict[RejectReason, int] = {
    "tool_adjudicated": 2,
    "navigational_abandon": 1,
}


@dataclass
class RejectionRecord:
    """A single rejection entry in the navigation state ledger.

    Two reasons are distinguished:

    - ``tool_adjudicated``: a SEARCH_* tool reconciliation proved the path has
      no matching assets. Strong (content-level) negative signal. Not revived
      this round; future strong-signal revival is a TODO.
    - ``navigational_abandon``: BACK left an unproductive scope. Weak (soft)
      signal; any discovery / lexical hit revives the path.
    """

    path: str
    reason: RejectReason
    step: int
    detail: str = ""


@dataclass
class NavigationState:
    """Mutable state for one document navigation loop.

    Path state is tracked in two orthogonal dimensions:

    - **Coverage** (positive "already taken as evidence"): derived from
      ``collected_paths`` via :meth:`covered_paths` / :meth:`outline_paths`.
    - **Rejection** (negative "evaluated, not taken"): a single labelled
      ledger in :attr:`rejected` keyed by normalized path. Replaces the
      former ``rejected_paths`` / ``rejected_collect_paths`` dual sets.
    """

    document_id: str
    document_name: str
    job_result_id: str
    current_scope: str | None = None
    expanded_scopes: set[str] = field(default_factory=set)
    rejected: dict[str, RejectionRecord] = field(default_factory=dict)
    collected_paths: list[dict[str, Any]] = field(default_factory=list)
    nav_trace: list[dict[str, Any]] = field(default_factory=list)
    tool_history: list[dict[str, Any]] = field(default_factory=list)
    blocked_asset_searches: set[str] = field(default_factory=set)
    step_count: int = 0

    # ── Coverage helpers (single source: collected_paths) ────────────────

    def covered_paths(self) -> set[str]:
        """Full-evidence paths (hydrate_mode != 'outline')."""
        return {
            PathLedger.normalize(str(item.get("path") or ""))
            for item in self.collected_paths
            if item.get("path") and item.get("hydrate_mode") != "outline"
        }

    def outline_paths(self) -> set[str]:
        """Outline-only paths; excludes any path also collected as full."""
        full = self.covered_paths()
        return {
            PathLedger.normalize(str(item.get("path") or ""))
            for item in self.collected_paths
            if item.get("path")
            and item.get("hydrate_mode") == "outline"
            and PathLedger.normalize(str(item.get("path") or "")) not in full
        }

    # ── Snapshot / delta for replayable traces ──────────────────────────

    def snapshot_delta(
        self,
        *,
        before_scope: str | None,
        expanded_before: set[str],
        rejected_before: dict[str, RejectionRecord],
        collected_before_count: int,
    ) -> dict[str, Any]:
        rejected_added: list[dict[str, Any]] = []
        for path, record in self.rejected.items():
            if path in rejected_before:
                continue
            rejected_added.append({
                "path": record.path,
                "reason": record.reason,
                "step": record.step,
                "detail": record.detail,
            })
        rejected_added.sort(key=lambda item: (item["step"], item["path"]))
        return {
            "current_scope_before": before_scope or "root",
            "current_scope_after": self.current_scope or "root",
            "expanded_added": sorted(self.expanded_scopes - expanded_before),
            "rejected_added": rejected_added,
            "collected_added": [
                item.get("path", "")
                for item in self.collected_paths[collected_before_count:]
                if item.get("path")
            ],
        }

    # ── Mutation helpers ────────────────────────────────────────────────

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

    def mark_rejected_collect(
        self,
        path: str | None,
        *,
        step: int,
        detail: str = "",
    ) -> None:
        """Record a tool-adjudicated rejection (strong, content-level)."""
        normalized = PathLedger.normalize(path)
        if not normalized:
            return
        self._upsert_rejection(
            normalized,
            reason="tool_adjudicated",
            step=step,
            detail=detail,
        )

    def mark_rejected_if_unproductive(
        self,
        path: str | None,
        *,
        step: int,
        detail: str = "",
    ) -> None:
        """Record a soft navigational abandon when leaving an unproductive scope.

        Only written when no stronger record exists for the path.
        """
        normalized = PathLedger.normalize(path)
        if not normalized:
            return
        has_full_collect = any(
            item.get("hydrate_mode") != "outline"
            and PathLedger.is_same_or_descendant(item.get("path"), normalized)
            for item in self.collected_paths
        )
        if has_full_collect:
            return
        self._upsert_rejection(
            normalized,
            reason="navigational_abandon",
            step=step,
            detail=detail,
        )

    def _upsert_rejection(
        self,
        normalized_path: str,
        *,
        reason: RejectReason,
        step: int,
        detail: str,
    ) -> None:
        existing = self.rejected.get(normalized_path)
        if existing is not None and _REASON_STRENGTH[existing.reason] >= _REASON_STRENGTH[reason]:
            # Keep the stronger prior record.
            return
        self.rejected[normalized_path] = RejectionRecord(
            path=normalized_path,
            reason=reason,
            step=step,
            detail=detail,
        )

    def rejected_paths_with_reason(self, reason: RejectReason) -> set[str]:
        """All paths rejected with a specific reason label."""
        return {
            path for path, record in self.rejected.items()
            if record.reason == reason
        }

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
