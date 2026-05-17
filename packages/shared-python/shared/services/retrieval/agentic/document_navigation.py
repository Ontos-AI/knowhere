"""Per-document navigation for agentic retrieval."""
from __future__ import annotations

from typing import Any, cast

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic import tools
from shared.services.retrieval.agentic.budget import BudgetExceeded
from shared.services.retrieval.agentic.evidence import reconcile_deferred_assets
from shared.services.retrieval.agentic.runtime import AgentLlmBudget
from shared.services.retrieval.agentic.trace import TraceRecorder
from shared.services.retrieval.agentic.types import (
    AgentRunConfig,
    AgentState,
    CandidateDoc,
    DocTreeNode,
    ToolResult,
)
from shared.services.retrieval.llm_adapter import LLMFn


class DocumentNavigationRunner:
    def __init__(
        self,
        *,
        db: AsyncSession,
        state: AgentState,
        trace: TraceRecorder,
        trace_enabled: bool,
        user_id: str,
        namespace: str,
        query: str,
        config: AgentRunConfig,
        discovery_by_doc: dict[str, list[dict[str, Any]]],
        llm_fn: LLMFn | None,
        llm_budget: AgentLlmBudget,
    ) -> None:
        self._db = db
        self._state = state
        self._trace = trace
        self._trace_enabled = trace_enabled
        self._user_id = user_id
        self._namespace = namespace
        self._query = query
        self._config = config
        self._discovery_by_doc = discovery_by_doc
        self._llm_fn = llm_fn
        self._llm_budget = llm_budget

    async def navigate_selected_documents(self, *, revision_hint: str | None) -> None:
        logger.info(
            f"  agentic: Phase 2 — navigating {len(self._state.selected_docs)} documents"
        )
        for doc in self._state.selected_docs:
            if self._state.elapsed_ms >= self._config.latency_budget_ms:
                logger.info("  agentic: latency budget hit during Phase 2, stopping")
                break
            await self._navigate_document(doc, revision_hint=revision_hint)

    async def _navigate_document(
        self,
        doc: CandidateDoc,
        *,
        revision_hint: str | None,
    ) -> None:
        job_result_id = self._state.doc_job_map.get(doc.document_id, "")
        if not job_result_id:
            logger.info(f"  agentic: skipping doc {doc.document_id} — no job_result_id")
            self._state.ever_explored_doc_ids.add(doc.document_id)
            return

        doc_name = doc.source_file_name or self._state.doc_id_to_name.get(doc.document_id, "")
        is_discovery_only_doc = doc.source == "discovery_auto"
        root = DocTreeNode(scope_path=None)
        doc_pending_assets: list[dict[str, Any]] = []

        if not is_discovery_only_doc:
            doc_pending_assets = await self._navigate_bfs(
                doc=doc,
                root=root,
                doc_name=doc_name,
                job_result_id=job_result_id,
                revision_hint=revision_hint,
            )

        await self._hydrate_discovery_hints(
            doc=doc,
            root=root,
            doc_name=doc_name,
            revision_hint=revision_hint,
        )

        if not is_discovery_only_doc and doc_pending_assets:
            self._reconcile_pending_assets(
                doc=doc,
                root=root,
                doc_name=doc_name,
                doc_pending_assets=doc_pending_assets,
            )

        if doc.document_id in self._state.doc_trees:
            self._state.doc_trees[doc.document_id].merge(root)
        else:
            self._state.doc_trees[doc.document_id] = root
        self._state.ever_explored_doc_ids.add(doc.document_id)
        if self._state.ledger is not None:
            self._state.ledger.mark_explored(docs=1)

    async def _navigate_bfs(
        self,
        *,
        doc: CandidateDoc,
        root: DocTreeNode,
        doc_name: str,
        job_result_id: str,
        revision_hint: str | None,
    ) -> list[dict[str, Any]]:
        doc_exclude: set[str] = {
            key.split("::", 1)[1]
            for key in self._state.seen_section_keys
            if key.startswith(f"{doc.document_id}::")
        } if self._state.seen_section_keys else set()
        pending: list[tuple[str | list[str] | None, DocTreeNode, int]] = [(None, root, 0)]
        doc_pending_assets: list[dict[str, Any]] = []

        while pending:
            if self._state.elapsed_ms >= self._config.latency_budget_ms:
                break

            scope, parent_node, depth = pending.pop(0)
            if depth >= self._config.max_nav_depth:
                continue
            if self._llm_fn is None:
                break
            if self._state.ledger and self._state.ledger.status("planning") in ("CRITICAL", "EXHAUSTED"):
                logger.info("  agentic: planning budget critical, ending BFS for current doc")
                break

            doc_llm_fn = self._llm_budget.for_document(
                cast(LLMFn, self._llm_fn),
                doc_id=doc.document_id,
                depth=depth,
            )
            try:
                action, asset_tools, step_node, drill_paths = await tools.navigate_step(
                    self._db,
                    document_id=doc.document_id,
                    job_result_id=job_result_id,
                    query=self._query,
                    llm_fn=doc_llm_fn,
                    user_id=self._user_id,
                    namespace=self._namespace,
                    doc_name=doc_name,
                    scope_path=scope,
                    exclude_paths=doc_exclude,
                    revision_hint=revision_hint if depth == 0 else None,
                    budget_snapshot=self._state.ledger.snapshot() if self._state.ledger else None,
                )
            except BudgetExceeded:
                logger.info("  agentic: planning budget exhausted during navigation")
                if self._trace_enabled:
                    self._trace.record_budget_stop("planning_exhausted")
                break
            self._state.step_count += 1

            await self._collect_assets(
                doc=doc,
                scope=scope,
                step_node=step_node,
                asset_tools=asset_tools,
                pending_assets=doc_pending_assets,
                round_scope="nav",
            )
            _merge_step_node(parent_node, step_node)
            _update_excluded_leaf_paths(doc_exclude, step_node, drill_paths)
            _queue_drill_paths(pending, parent_node, drill_paths, depth)
            parent_node.reparent_leaf_content()
            self._record_navigation_step(
                doc=doc,
                scope=scope,
                depth=depth,
                action=action,
                asset_tools=asset_tools,
                step_node=step_node,
                drill_paths=drill_paths,
            )
            if self._state.ledger is not None:
                self._state.ledger.mark_explored(
                    chunks=sum(len(chunks) for chunks in step_node.leaf_content.values()),
                )

        return doc_pending_assets

    async def _collect_assets(
        self,
        *,
        doc: CandidateDoc,
        scope: str | list[str] | None,
        step_node: DocTreeNode,
        asset_tools: list[str],
        pending_assets: list[dict[str, Any]],
        round_scope: str,
    ) -> None:
        selected_asset_scopes = list(step_node.confidence.keys())
        asset_scope = selected_asset_scopes or scope
        for asset_tool in asset_tools:
            if asset_tool not in ("FIND_IMAGES", "FIND_TABLES"):
                continue
            asset_type = "image" if asset_tool == "FIND_IMAGES" else "table"
            asset_chunks = await tools.asset_filter_step(
                self._db,
                document_id=doc.document_id,
                job_result_id=self._state.doc_job_map.get(doc.document_id, ""),
                scope_path=asset_scope,
                asset_type=asset_type,
            )
            if asset_chunks:
                pending_assets.extend(asset_chunks)

            scope_display = (
                asset_scope
                if isinstance(asset_scope, list)
                else (asset_scope or "root")
            )
            if self._trace_enabled:
                self._trace.record_step(
                    "asset_filter_step",
                    ToolResult(
                        status="filtered" if asset_chunks else "empty",
                        payload={
                            "document_id": doc.document_id,
                            "scope": scope_display,
                            "navigation_scope": scope if isinstance(scope, str) else (scope or "root"),
                            "asset_type": asset_type,
                            "chunks_found": len(asset_chunks) if asset_chunks else 0,
                        },
                    ),
                    decision_reason=f"asset_{round_scope}_{doc.source_file_name}",
                )
            logger.info(
                f"  agentic step {self._state.step_count}: asset_filter_step "
                f'doc="{doc.source_file_name}" scope={scope_display} '
                f"type={asset_type} chunks={len(asset_chunks) if asset_chunks else 0}"
            )

    async def _hydrate_discovery_hints(
        self,
        *,
        doc: CandidateDoc,
        root: DocTreeNode,
        doc_name: str,
        revision_hint: str | None,
    ) -> None:
        doc_hints = self._discovery_by_doc.get(doc.document_id, [])
        if not doc_hints or self._llm_fn is None:
            return
        if self._state.elapsed_ms >= self._config.latency_budget_ms:
            return

        discovery_exclude_paths = {
            key.split("::", 1)[1]
            for key in root.collect_all_paths(doc.document_id)
        }
        doc_discovery_llm_fn = self._llm_budget.for_discovery(
            cast(LLMFn, self._llm_fn),
            doc_id=doc.document_id,
            low_priority=root.has_content(),
        )
        try:
            discovery_node = await tools.discovery_select_step(
                self._db,
                document_id=doc.document_id,
                query=self._query,
                llm_fn=doc_discovery_llm_fn,
                user_id=self._user_id,
                namespace=self._namespace,
                doc_name=doc_name,
                discovery_hints=doc_hints,
                exclude_paths=discovery_exclude_paths,
                revision_hint=revision_hint,
                budget_snapshot=self._state.ledger.snapshot() if self._state.ledger else None,
            )
        except BudgetExceeded:
            logger.info("  agentic: planning budget exhausted during discovery selection")
            if self._trace_enabled:
                self._trace.record_budget_stop("planning_exhausted")
            discovery_node = DocTreeNode(scope_path=None)
        self._state.step_count += 1

        if self._trace_enabled:
            self._trace.record_step(
                "discovery_select_step",
                ToolResult(
                    status="selected" if discovery_node.has_content() else "empty",
                    payload={
                        "document_id": doc.document_id,
                        "hints_count": len(doc_hints),
                        "hydrated_count": len(discovery_node.leaf_content),
                    },
                ),
                decision_reason=f"discovery_{doc.source_file_name}",
            )
        root.merge(discovery_node)
        if self._state.ledger is not None:
            self._state.ledger.mark_explored(
                chunks=sum(len(chunks) for chunks in discovery_node.leaf_content.values()),
            )

    def _reconcile_pending_assets(
        self,
        *,
        doc: CandidateDoc,
        root: DocTreeNode,
        doc_name: str,
        doc_pending_assets: list[dict[str, Any]],
    ) -> None:
        if doc_name and not root.children and not any(
            item.get("path") == doc_name for item in root.outline_items
        ):
            root.outline_items.insert(0, {"path": doc_name, "level": 0})
        reconcile_deferred_assets(root, doc_pending_assets)
        if self._trace_enabled:
            self._trace.record_step(
                "deferred_asset_reconcile",
                ToolResult(
                    status="reconciled",
                    payload={
                        "document_id": doc.document_id,
                        "pending_count": len(doc_pending_assets),
                        "placed_count": sum(
                            1 for asset in doc_pending_assets
                            if str(asset.get("chunk_id") or "") in {
                                str(row.get("chunk_id") or "")
                                for row in root.flatten_chunk_rows()
                            }
                        ),
                    },
                ),
                decision_reason=f"deferred_reconcile_{doc.source_file_name}",
            )

    def _record_navigation_step(
        self,
        *,
        doc: CandidateDoc,
        scope: str | list[str] | None,
        depth: int,
        action: str,
        asset_tools: list[str],
        step_node: DocTreeNode,
        drill_paths: list[dict[str, Any]],
    ) -> None:
        if self._trace_enabled:
            self._trace.record_step(
                "navigate_step",
                ToolResult(
                    status=f"{action.lower()}" + (" (content)" if step_node.has_content() else ""),
                    payload={
                        "document_id": doc.document_id,
                        "scope": scope if isinstance(scope, str) else (scope or "root"),
                        "depth": depth,
                        "action": action,
                        "asset_tools": asset_tools,
                        "outline_count": len(step_node.outline_items),
                        "leaf_count": len(step_node.leaf_content),
                        "pending_drills": len(drill_paths),
                    },
                ),
                decision_reason=f"nav_d{depth}_{doc.source_file_name}",
            )
        scope_log = scope if isinstance(scope, str) else (", ".join(scope) if scope else "root")
        logger.info(
            f"  agentic step {self._state.step_count}: navigate_step "
            f'doc="{doc.source_file_name}" scope={scope_log} '
            f"depth={depth} action={action} tools={asset_tools} "
            f"outline={len(step_node.outline_items)} "
            f"leaves={len(step_node.leaf_content)} "
            f"drills={len(drill_paths)}"
        )


def _merge_step_node(parent_node: DocTreeNode, step_node: DocTreeNode) -> None:
    parent_node.outline_items = step_node.outline_items
    for leaf_path, chunks in step_node.leaf_content.items():
        parent_node.add_leaf_chunks(leaf_path, chunks)
    parent_node.confidence = step_node.confidence


def _update_excluded_leaf_paths(
    doc_exclude: set[str],
    step_node: DocTreeNode,
    drill_paths: list[dict[str, Any]],
) -> None:
    drill_path_set = {str(selection["path"]) for selection in drill_paths}
    for leaf_path in step_node.leaf_content:
        if leaf_path not in drill_path_set:
            doc_exclude.add(leaf_path)


def _queue_drill_paths(
    pending: list[tuple[str | list[str] | None, DocTreeNode, int]],
    parent_node: DocTreeNode,
    drill_paths: list[dict[str, Any]],
    depth: int,
) -> None:
    if not drill_paths:
        return
    for selection in drill_paths:
        child = DocTreeNode(scope_path=selection["path"])
        parent_node.children[selection["path"]] = child
    batch_scope = [selection["path"] for selection in drill_paths]
    pending.append((batch_scope, parent_node, depth + 1))
