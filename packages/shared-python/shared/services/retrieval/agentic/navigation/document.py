"""Per-document navigation for agentic retrieval.

Collector Agent architecture
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The navigation loop uses a Collector Agent model where each step
independently produces two decisions:

1. **collect**: paths to add to the evidence collection
2. **action**: navigation direction (DRILL/BACK/STOP)

The ``collected_paths`` list accumulates across all steps.  After
navigation completes (or is interrupted), a single batch hydration
pass loads content for all collected paths.

Asset collection (images/tables) still runs during navigation so LLM
tool requests are honoured, but assets are reconciled after hydration.
"""
from __future__ import annotations

from typing import Any, cast

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic import tools
from shared.services.retrieval.agentic.core.budget import BudgetExceeded
from shared.services.retrieval.agentic.evidence.builder import reconcile_deferred_assets
from shared.services.retrieval.agentic.core.runtime import AgentLlmBudget
from shared.services.retrieval.agentic.core.trace import TraceRecorder
from shared.services.retrieval.agentic.core.types import (
    AgentRunConfig,
    AgentState,
    CandidateDoc,
    DocTreeNode,
    NavigateStepResult,
    ToolResult,
)
from shared.services.retrieval.agentic.navigation.selection_hydration import (
    hydrate_path_selections_into_node,
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
        self._decision_steps: list[dict[str, Any]] = []

    @property
    def decision_steps(self) -> list[dict[str, Any]]:
        return list(self._decision_steps)

    async def navigate_selected_documents(self) -> None:
        logger.info(
            f"  agentic: Phase 2 — navigating {len(self._state.selected_docs)} documents"
        )
        for doc in self._state.selected_docs:
            if self._state.elapsed_ms >= self._config.latency_budget_ms:
                logger.info("  agentic: latency budget hit during Phase 2, stopping")
                break
            await self._navigate_document(doc)

    async def _navigate_document(
        self,
        doc: CandidateDoc,
    ) -> None:
        job_result_id = self._state.doc_job_map.get(doc.document_id, "")
        if not job_result_id:
            logger.info(f"  agentic: skipping doc {doc.document_id} — no job_result_id")
            self._state.ever_explored_doc_ids.add(doc.document_id)
            return

        doc_name = doc.source_file_name or self._state.doc_id_to_name.get(doc.document_id, "")
        root = DocTreeNode(scope_path=None)
        doc_pending_assets: list[dict[str, Any]] = []

        # Phase 2A: Collector Agent navigation (summary-only, no content hydration)
        doc_pending_assets, collected_paths = await self._navigate_collector(
            doc=doc,
            root=root,
            doc_name=doc_name,
            job_result_id=job_result_id,
        )

        # Phase 2B: Discovery hints (independent hydration path)
        await self._hydrate_discovery_hints(
            doc=doc,
            root=root,
            doc_name=doc_name,
            collected_paths=collected_paths,
        )

        # Phase 2C: Batch hydrate all collected paths
        if collected_paths:
            await self._hydrate_collected(
                doc=doc,
                root=root,
                job_result_id=job_result_id,
                collected_paths=collected_paths,
            )

        # Phase 2D: Reconcile assets into hydrated tree
        if doc_pending_assets:
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

    async def _navigate_collector(
        self,
        *,
        doc: CandidateDoc,
        root: DocTreeNode,
        doc_name: str,
        job_result_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Collector Agent navigation loop.

        Returns (doc_pending_assets, collected_paths).
        """
        doc_exclude: set[str] = set()
        nav_trace: list[dict[str, Any]] = []
        collected_paths: list[dict[str, Any]] = []
        doc_pending_assets: list[dict[str, Any]] = []

        # Current navigation scope (None = root)
        current_scope: str | None = None
        step_count = 0

        # Context from SEARCH tools — injected into next navigate prompt
        search_context: str = ""

        exit_reason = "unknown"

        while step_count < self._config.max_nav_steps:
            if self._state.elapsed_ms >= self._config.latency_budget_ms:
                exit_reason = "latency"
                break
            if self._state.ledger and self._state.ledger.status("planning") in ("CRITICAL", "EXHAUSTED"):
                logger.info("  agentic: planning budget critical, ending navigation for current doc")
                exit_reason = "budget"
                break

            step_count += 1

            doc_llm_fn = self._llm_budget.for_document(
                cast(LLMFn, self._llm_fn),
                doc_id=doc.document_id,
                step=step_count,
            )
            try:
                nav_result = await tools.navigate_step(
                    self._db,
                    document_id=doc.document_id,
                    job_result_id=job_result_id,
                    query=self._query,
                    llm_fn=doc_llm_fn,
                    user_id=self._user_id,
                    namespace=self._namespace,
                    doc_name=doc_name,
                    scope_path=current_scope,
                    exclude_paths=doc_exclude,
                    budget_snapshot=self._state.ledger.snapshot() if self._state.ledger else None,
                    nav_trace=nav_trace if nav_trace else None,
                    collected_paths=collected_paths,
                    search_context=search_context,
                )
            except BudgetExceeded:
                logger.info("  agentic: planning budget exhausted during navigation")
                if self._trace_enabled:
                    self._trace.record_budget_stop("planning_exhausted")
                exit_reason = "budget"
                break
            self._state.step_count += 1

            # Clear previous tool context (consumed by this step's prompt)
            search_context = ""
            has_matched_assets = False

            # ── Execute asset tools (SEARCH) ─────────────────────────────
            search_context, has_matched_assets = await self._execute_asset_tools(
                doc=doc,
                job_result_id=job_result_id,
                scope=current_scope,
                nav_result=nav_result,
                pending_assets=doc_pending_assets,
            )

            # Merge outline + confidence into root tree
            _merge_step_node(root, nav_result.node)

            # ── Process COLLECT ──────────────────────────────────────────
            collected_in_step: list[str] = []
            for coll_item in nav_result.collect:
                path = coll_item["path"]
                coll_item["collected_at_step"] = step_count
                coll_item["scope_context"] = current_scope or "root"
                collected_paths.append(coll_item)
                collected_in_step.append(path)
                # Outline collections should NOT exclude children — the intent
                # is "see structure, then drill deeper for full content".
                if coll_item.get("hydrate_mode") != "outline":
                    doc_exclude.add(path)

            # ── Build trace entry ────────────────────────────────────────
            trace_entry: dict[str, Any] = {
                "step": step_count,
                "scope": current_scope or "root",
                "action": nav_result.action,
                "drill_into": nav_result.drill_into,
                "collected": collected_in_step,
                "tools_used": nav_result.tools,
                "reason": nav_result.reason,
            }
            if nav_result.fallback_reason:
                trace_entry["fallback_reason"] = nav_result.fallback_reason
            # Record tool usage & results so future steps can see search history
            if nav_result.tools and nav_result.search_assets_params:
                trace_entry["tool_results"] = {
                    "tool": nav_result.tools[0],
                    "query": nav_result.search_assets_params.get("query", ""),
                    "matched": has_matched_assets,
                }
            nav_trace.append(trace_entry)

            # ── Record decision step ─────────────────────────────────────
            self._record_navigation_step(
                doc=doc,
                scope=current_scope,
                step_num=step_count,
                nav_result=nav_result,
                collected_in_step=collected_in_step,
            )

            # ── Process navigation action ────────────────────────────────
            if nav_result.action == "DRILL" and nav_result.drill_into:
                drill_path = nav_result.drill_into
                # Create child node in tree for the drill target
                target_parent = _find_target_node(root, drill_path)
                target_parent.children.setdefault(drill_path, DocTreeNode(scope_path=drill_path))
                current_scope = drill_path

            elif nav_result.action == "BACK":
                if current_scope is None:
                    # Already at root — nowhere to go back
                    logger.info("  agentic: BACK at root scope, treating as STOP")
                    exit_reason = "llm_stop"
                    break

                back_target = nav_result.back_to  # None = root
                if _is_valid_ancestor(current_scope, back_target):
                    current_scope = back_target
                else:
                    # Invalid back_to — fallback: go to path parent
                    path_parent = (
                        current_scope.rsplit(" / ", 1)[0]
                        if " / " in current_scope
                        else None
                    )
                    logger.warning(
                        f"  agentic: invalid back_to='{back_target}' "
                        f"from scope='{current_scope}', "
                        f"falling back to path parent='{path_parent or 'root'}'"
                    )
                    current_scope = path_parent

            elif nav_result.action == "ERROR":
                logger.warning(
                    f"  agentic: navigation ERROR for doc={doc.document_id}: "
                    f"{nav_result.error_reason or nav_result.reason}"
                )
                exit_reason = "error"
                break

            elif nav_result.action == "STOP" or nav_result.is_terminal:
                # If asset tools found actual matches, override STOP and
                # continue one more round so the LLM can see the results.
                # "No matching" context alone does NOT override STOP.
                if search_context and has_matched_assets:
                    logger.info(
                        "  agentic: STOP overridden — asset tool found matches, "
                        "continuing navigation to present tool output"
                    )
                else:
                    exit_reason = "llm_stop"
                    break
        else:
            # while loop exhausted — max_nav_steps reached
            exit_reason = "max_steps"

        # Fallback: if navigation was forcefully interrupted and collected
        # nothing, auto-collect visible leaf children under the last explored
        # scope.  When LLM voluntarily STOPs or BACKs to root with empty
        # collection, respect its judgment — it determined there is no
        # relevant evidence.
        #   budget    – planning pool CRITICAL/EXHAUSTED (pre-check or exception)
        #   latency   – elapsed time exceeded latency budget
        #   max_steps – navigation step count limit reached
        #   error     – unexpected exception during navigate_step
        forced_exits = ("budget", "latency", "max_steps", "error")
        fallback_triggered = False
        if not collected_paths and step_count > 0 and exit_reason in forced_exits:
            fallback_triggered = True
            fallback_scope = current_scope
            logger.info(
                f"  agentic: forced exit ({exit_reason}) with 0 collected paths, "
                f"auto-collecting leaves under scope={fallback_scope or 'root'}"
            )
            from shared.services.retrieval.agentic.navigation.section_tree import (
                load_child_sections,
            )
            fallback_items = await load_child_sections(
                self._db,
                doc.document_id,
                job_result_id,
                fallback_scope,
            )
            for item in fallback_items:
                if not item.get("show_summary", True):
                    continue
                if item.get("is_leaf"):
                    path = item["path"]
                    collected_paths.append({
                        "path": path,
                        "confidence": 0.4,
                        "hydrate_mode": "chunks",
                        "collected_at_step": step_count,
                        "scope_context": fallback_scope or "root",
                        "fallback_reason": f"forced_exit_{exit_reason}",
                    })

            if collected_paths and self._trace_enabled:
                self._trace.record_fallback(
                    "forced_exit_auto_collect",
                    f"exit_reason={exit_reason}, collected {len(collected_paths)} "
                    f"leaf paths under scope={fallback_scope or 'root'}",
                )

            # Record fallback in decision_steps for downstream visibility
            self._decision_steps.append({
                "phase": "navigate_fallback",
                "document": doc.source_file_name or "",
                "document_id": doc.document_id,
                "action": "auto_collect",
                "reason": (
                    f"Forced exit ({exit_reason}) with 0 collected paths. "
                    f"Auto-collected {len(collected_paths)} leaf paths "
                    f"under scope={fallback_scope or 'root'}."
                ),
                "exit_reason": exit_reason,
                "fallback_scope": fallback_scope or "root",
                "collected_count": len(collected_paths),
                "collected_paths": [p["path"] for p in collected_paths],
            })

        # ── Navigate summary — record exit reason and final state ─────
        doc_name = doc.source_file_name or self._state.doc_id_to_name.get(doc.document_id, "")
        self._decision_steps.append({
            "phase": "navigate_summary",
            "document": doc_name,
            "document_id": doc.document_id,
            "action": exit_reason,
            "reason": (
                f"Navigation ended: exit_reason={exit_reason}, "
                f"steps={step_count}, collected={len(collected_paths)}, "
                f"fallback={'yes' if fallback_triggered else 'no'}"
            ),
            "exit_reason": exit_reason,
            "total_steps": step_count,
            "collected_count": len(collected_paths),
            "fallback_triggered": fallback_triggered,
            "final_scope": current_scope or "root",
        })

        return doc_pending_assets, collected_paths

    async def _hydrate_collected(
        self,
        *,
        doc: CandidateDoc,
        root: DocTreeNode,
        job_result_id: str,
        collected_paths: list[dict[str, Any]],
    ) -> None:
        """Batch-hydrate all collected paths after navigation completes."""
        if not collected_paths:
            return

        # Deduplicate: keep highest confidence per path
        deduped: dict[str, dict[str, Any]] = {}
        for item in collected_paths:
            path = item["path"]
            if path not in deduped or item.get("confidence", 0) > deduped[path].get("confidence", 0):
                deduped[path] = item
        unique_selections = list(deduped.values())

        # Ensure child nodes exist for each collected path so reparent can
        # correctly route descendant chunks into the right subtree.
        for item in unique_selections:
            path = item["path"]
            _ensure_child_node(root, path)

        await hydrate_path_selections_into_node(
            self._db,
            node=root,
            path_selections=unique_selections,
            user_id=self._user_id,
            namespace=self._namespace,
            document_id=doc.document_id,
            job_result_id=job_result_id,
        )

        # Single reparent pass — tree structure is final.
        root.reparent_leaf_content()

        # Load section tree outline for each collected child node, then
        # build a proper sub-tree so the renderer can nest L3 under L2 etc.
        from shared.services.retrieval.agentic.navigation.section_tree import load_child_sections
        for item in unique_selections:
            path = item["path"]
            child_node = root.children.get(path)
            if child_node is None or child_node.outline_items:
                continue  # Skip if no child or already has outline
            try:
                section_items = await load_child_sections(
                    self._db, doc.document_id, job_result_id, path,
                    limit_depth=False,
                )
                if section_items:
                    # Filter out the scope node itself AND ancestor/sibling
                    # items. load_child_sections returns ancestor context
                    # for navigation prompts, but for evidence rendering the
                    # child node only needs its own descendants.
                    child_node.outline_items = [
                        si for si in section_items
                        if si.get("path") != path
                        and si.get("path", "").startswith(path + " / ")
                    ]
                    # Build sub-tree from outline hierarchy and re-reparent
                    # so chunks are correctly nested (e.g. L3 under L2).
                    _build_outline_subtree(child_node)
            except Exception as exc:
                logger.warning(f"  hydrate_collected: failed to load outline for '{path}': {exc}")

        # Also organize any discovery chunks sitting in root.leaf_content.
        # Same path-based tree building; pre-existing children are protected.
        if root.leaf_content:
            _build_outline_subtree(root)

        # Accurate budget accounting — count only actually-hydrated chunks.
        if self._state.ledger is not None:
            total_chunks = len(root.flatten_chunk_rows())
            self._state.ledger.mark_explored(chunks=total_chunks)
            logger.info(
                f"  agentic: hydrate_collected doc={doc.document_id} "
                f"collected={len(unique_selections)} hydrated_chunks={total_chunks}"
            )

    async def _execute_asset_tools(
        self,
        *,
        doc: CandidateDoc,
        job_result_id: str,
        scope: str | None,
        nav_result: NavigateStepResult,
        pending_assets: list[dict[str, Any]],
    ) -> tuple[str, bool]:
        """Execute asset tools requested by the Navigator.

        Handles SEARCH_IMAGES and SEARCH_TABLES.
        Returns (search_context, has_matched_assets) where:
        - search_context is injected into the next navigate prompt
        - has_matched_assets indicates whether actual matches were found
        """
        search_context = ""
        has_matched = False

        if not nav_result.tools:
            return search_context, has_matched

        # ── SEARCH_IMAGES / SEARCH_TABLES ────────────────────────────
        if nav_result.search_assets_params:
            params = nav_result.search_assets_params
            search_query = params["query"]
            asset_type = params["asset_type"]
            tool_name = "SEARCH_IMAGES" if asset_type == "image" else "SEARCH_TABLES"

            search_llm_fn = self._llm_budget.for_document(
                cast(LLMFn, self._llm_fn),
                doc_id=doc.document_id,
                step=self._state.step_count,
            ) if self._llm_fn else None

            # Create VLM function for image search (lazy — only when needed)
            vlm_fn: LLMFn | None = None
            if asset_type == "image":
                from shared.services.retrieval.llm_adapter import create_retrieval_vlm_fn
                vlm_fn = create_retrieval_vlm_fn()

            if search_llm_fn:
                try:
                    matched_assets = await tools.search_assets_step(
                        self._db,
                        document_id=doc.document_id,
                        job_result_id=job_result_id,
                        scope_path=scope,
                        asset_type=asset_type,
                        query=search_query,
                        llm_fn=search_llm_fn,
                        vlm_fn=vlm_fn,
                    )
                except BudgetExceeded:
                    logger.info(
                        f"  agentic: {tool_name} skipped — planning budget exhausted"
                    )
                    search_context = (
                        f"=== {tool_name} Results ===\n"
                        f"{tool_name} skipped: planning budget exhausted.\n"
                        f"=== End {tool_name} Results ==="
                    )
                    if self._trace_enabled:
                        self._trace.record_step(
                            "search_assets_step",
                            ToolResult(
                                status="budget_exceeded",
                                payload={
                                    "document_id": doc.document_id,
                                    "scope": scope or "root",
                                    "asset_type": asset_type,
                                    "query": search_query,
                                },
                            ),
                            decision_reason=f"search_{asset_type}_{doc.source_file_name}",
                        )
                    return search_context, has_matched

                if matched_assets:
                    has_matched = True
                    # Add matched assets to pending_assets for reconciliation
                    pending_assets.extend(matched_assets)

                    # Build context for next navigate prompt
                    lines = [
                        f"=== {tool_name} Results ===",
                        f'Found {len(matched_assets)} matching {asset_type}s for "{search_query}":',
                    ]
                    for i, asset in enumerate(matched_assets[:10]):
                        chunk_id = asset.get("chunk_id", "")
                        file_path = asset.get("file_path", "")
                        owner = asset.get("owner_section_path", "")
                        metadata = asset.get("chunk_metadata") or {}
                        desc = (metadata.get("summary") or asset.get("content", ""))[:120]
                        lines.append(
                            f"  {i+1}. chunk_id=\"{chunk_id}\" file=\"{file_path}\""
                        )
                        lines.append(f"     section: {owner}")
                        lines.append(f"     {desc}")
                    lines.append(
                        "You can COLLECT the sections containing these assets."
                    )
                    lines.append(f"=== End {tool_name} Results ===")
                    search_context = "\n".join(lines)
                else:
                    search_context = (
                        f"=== {tool_name} Results ===\n"
                        f"No matching {asset_type}s found for \"{search_query}\".\n"
                        f"=== End {tool_name} Results ==="
                    )

                if self._trace_enabled:
                    self._trace.record_step(
                        "search_assets_step",
                        ToolResult(
                            status="matched" if matched_assets else "empty",
                            payload={
                                "document_id": doc.document_id,
                                "scope": scope or "root",
                                "asset_type": asset_type,
                                "query": search_query,
                                "total_matched": len(matched_assets) if matched_assets else 0,
                            },
                        ),
                        decision_reason=f"search_{asset_type}_{doc.source_file_name}",
                    )

                logger.info(
                    f"  agentic step {self._state.step_count}: {tool_name} "
                    f'doc="{doc.source_file_name}" scope={scope or "root"} '
                    f'query="{search_query[:50]}" '
                    f"matched={len(matched_assets) if matched_assets else 0}"
                )

        return search_context, has_matched

    async def _hydrate_discovery_hints(
        self,
        *,
        doc: CandidateDoc,
        root: DocTreeNode,
        doc_name: str,
        collected_paths: list[dict[str, Any]] | None = None,
    ) -> None:
        doc_hints = self._discovery_by_doc.get(doc.document_id, [])
        if not doc_hints or self._llm_fn is None:
            return
        if self._state.elapsed_ms >= self._config.latency_budget_ms:
            return

        discovery_exclude_paths = _build_discovery_exclude_set(
            root, collected_paths or []
        )

        doc_discovery_llm_fn = self._llm_budget.for_discovery(
            cast(LLMFn, self._llm_fn),
            doc_id=doc.document_id,
            low_priority=root.has_content(),
        )
        try:
            result = await tools.discovery_select_step(
                self._db,
                document_id=doc.document_id,
                query=self._query,
                llm_fn=doc_discovery_llm_fn,
                user_id=self._user_id,
                namespace=self._namespace,
                doc_name=doc_name,
                discovery_hints=doc_hints,
                exclude_paths=discovery_exclude_paths,
                budget_snapshot=self._state.ledger.snapshot() if self._state.ledger else None,
            )
        except BudgetExceeded:
            logger.info("  agentic: planning budget exhausted during discovery selection")
            if self._trace_enabled:
                self._trace.record_budget_stop("planning_exhausted")
            self._decision_steps.append({
                "phase": "discovery_select",
                "document": doc_name,
                "document_id": doc.document_id,
                "action": "budget_exceeded",
                "reason": "planning budget exhausted before LLM call",
                "candidate_count": len(doc_hints),
                "hydrated_count": 0,
                "selected_paths": [],
                "excluded_hints": [],
                "exclude_set": sorted(discovery_exclude_paths),
            })
            self._state.step_count += 1
            return
        self._state.step_count += 1

        discovery_node = result.node
        excluded_hints = result.excluded_hints

        if self._trace_enabled:
            self._trace.record_step(
                "discovery_select_step",
                ToolResult(
                    status="selected" if discovery_node.has_content() else "empty",
                    payload={
                        "document_id": doc.document_id,
                        "hints_count": len(doc_hints),
                        "hydrated_count": len(discovery_node.leaf_content),
                        "excluded_count": len(excluded_hints),
                    },
                ),
                decision_reason=f"discovery_{doc.source_file_name}",
            )
        self._decision_steps.append({
            "phase": "discovery_select",
            "document": doc_name,
            "document_id": doc.document_id,
            "action": "select" if discovery_node.has_content() else "skip",
            "reason": "",
            "candidate_count": result.candidate_count,
            "hydrated_count": len(discovery_node.leaf_content),
            "selected_paths": list(discovery_node.leaf_content.keys()),
            "excluded_hints": excluded_hints,
            "exclude_set": sorted(discovery_exclude_paths),
        })
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
        scope: str | None,
        step_num: int,
        nav_result: NavigateStepResult,
        collected_in_step: list[str],
    ) -> None:
        action = nav_result.action
        reason = nav_result.reason
        drill_into = nav_result.drill_into

        if self._trace_enabled:
            payload: dict[str, Any] = {
                "document_id": doc.document_id,
                "scope": scope or "root",
                "step": step_num,
                "action": action,
                "reason": reason,
                "drill_into": drill_into,
                "collected_count": len(collected_in_step),
                "collected_paths": collected_in_step,
                "asset_tools": nav_result.tools,
                "outline_count": len(nav_result.node.outline_items),
            }
            if nav_result.error_reason:
                payload["error_reason"] = nav_result.error_reason
            if nav_result.fallback_reason:
                payload["fallback_reason"] = nav_result.fallback_reason
            self._trace.record_step(
                "navigate_step",
                ToolResult(
                    status=f"{action.lower()}",
                    payload=payload,
                    error=nav_result.error_reason,
                ),
                decision_reason=f"nav_s{step_num}_{doc.source_file_name}",
            )

        doc_name = doc.source_file_name or self._state.doc_id_to_name.get(doc.document_id, "")
        step_entry: dict[str, Any] = {
            "phase": "navigate",
            "document": doc_name,
            "document_id": doc.document_id,
            "action": action,
            "reason": reason,
            "step": step_num,
            "drill_into": drill_into,
            "collected_paths": collected_in_step,
            "collected_count": len(collected_in_step),
        }
        if nav_result.error_reason:
            step_entry["error_reason"] = nav_result.error_reason
        if nav_result.fallback_reason:
            step_entry["fallback_reason"] = nav_result.fallback_reason
        self._decision_steps.append(step_entry)

        fallback_tag = f" FALLBACK={nav_result.fallback_reason}" if nav_result.fallback_reason else ""
        scope_log = scope or "root"
        logger.info(
            f"  agentic step {self._state.step_count}: navigate_step "
            f'doc="{doc.source_file_name}" scope={scope_log} '
            f"step={step_num} action={action} tools={nav_result.tools} "
            f'reason="{reason[:80]}" '
            f"collected={len(collected_in_step)} "
            f"drill_into={drill_into} "
            f"outline={len(nav_result.node.outline_items)}"
            f"{fallback_tag}"
        )


def _is_valid_ancestor(current_scope: str, target: str | None) -> bool:
    """Check if *target* is a valid ancestor of *current_scope*.

    Valid ancestors are:
    - ``None`` (root): always valid when current_scope is not None
    - Any proper prefix by ``" / "`` splitting

    E.g. for ``"A / B / C"``: valid = {None, "A", "A / B"}
    """
    if target is None:
        return True  # root is always a valid ancestor
    if not current_scope:
        return False  # can't go back from root
    # target must be a proper prefix of current_scope
    return current_scope.startswith(target + " / ")


def _find_target_node(node: DocTreeNode, path: str) -> DocTreeNode:
    """Walk the tree to find the deepest existing node that owns *path*.

    Only recurse when *path* is a true descendant of a child (prefix match).
    An exact match means the item belongs to the section itself, which is
    managed by the *parent* node — the renderer already handles the case
    where a path appears in both ``children`` and ``leaf_content``.
    """
    for child_path, child in node.children.items():
        if path.startswith(child_path + " / "):
            return _find_target_node(child, path)
    return node


def _merge_step_node(root: DocTreeNode, step_node: DocTreeNode) -> None:
    """Route outline items and confidence from *step_node* to correct tree positions."""
    for item in step_node.outline_items:
        path = item.get("path", "")
        target = _find_target_node(root, path)
        existing = {i.get("path") for i in target.outline_items}
        if path not in existing:
            target.outline_items.append(item)

    for path, conf in step_node.confidence.items():
        target = _find_target_node(root, path)
        target.confidence[path] = max(target.confidence.get(path, 0), conf)


def _collect_leaf_paths(node: DocTreeNode) -> set[str]:
    """Collect all paths that have been hydrated (leaf_content)."""
    paths = set(node.leaf_content.keys())
    for child in node.children.values():
        paths.update(_collect_leaf_paths(child))
    return paths


def _build_discovery_exclude_set(
    root: DocTreeNode,
    collected_paths: list[dict[str, Any]],
) -> set[str]:
    """Build exclude set for discovery using collected navigation paths.

    If navigation COLLECT'd a parent path like "五、施工安全保证措施",
    all discovery hints under that path should be excluded because
    COLLECT already loads all descendants via prefix matching.
    """
    # 1. Already-hydrated leaf paths
    exclude = _collect_leaf_paths(root)

    # 2. Collected parent paths from navigation COLLECT decisions.
    #    These haven't been hydrated yet (hydrate runs after discovery),
    #    but we know COLLECT will load all their descendants.
    for item in collected_paths:
        path = item.get("path", "")
        if path:
            exclude.add(path)

    return exclude



def _ensure_child_node(root: DocTreeNode, path: str) -> None:
    """Create an intermediate child node for *path* if it doesn't exist.

    When a non-leaf section is COLLECTed (e.g. "五、施工安全保证措施"),
    hydration loads all descendant chunks (e.g. "五、... / 3.监控量测措施 / 3.1...").
    These chunks are initially placed in root.leaf_content.
    ``reparent_leaf_content`` then moves them into the correct child subtree —
    but only if a child node exists for the collected path.

    This function creates that child node so reparent can work correctly.
    """
    # Don't create a child for root-level or if already exists
    if not path:
        return

    # Walk to find the deepest existing ancestor node
    target = root
    for child_path, child in root.children.items():
        if path.startswith(child_path + " / "):
            target = child
            break
        if path == child_path:
            return  # Already exists

    # Create the child node on the target
    if path not in target.children:
        target.children[path] = DocTreeNode(scope_path=path)


def _build_outline_subtree(node: DocTreeNode) -> None:
    """Recursively build child nodes from outline_items + leaf_content paths.

    Every section_path is ``" / "``-separated; parent-child is prefix match.
    This function creates children one level at a time, reparents chunks
    into them, then recurses so the next level is handled correctly.
    """
    if not node.outline_items and not node.leaf_content:
        return

    # All known paths: outline metadata + actual chunk paths.
    all_paths = (
        {item["path"] for item in node.outline_items}
        | set(node.leaf_content.keys())
    )

    # Find paths that have at least one descendant.
    parent_paths: set[str] = set()
    for path in all_paths:
        for other in all_paths:
            if other != path and other.startswith(path + " / "):
                parent_paths.add(path)
                break

    if not parent_paths:
        return  # All items are leaves — nothing to nest.

    # KEY FIX: only keep top-level parents.  If "A / B" and "A / B / C"
    # are both parents, only create "A / B" as a child NOW.  "A / B / C"
    # will be created when the function recurses into "A / B".
    parent_paths = {
        pp for pp in parent_paths
        if not any(pp != other and pp.startswith(other + " / ") for other in parent_paths)
    }

    # Track children that already exist (e.g. from collected hydration).
    # Their outline_items are already populated — don't push duplicates.
    pre_existing = set(node.children.keys())

    # Create child nodes for top-level parent paths only.
    for path in parent_paths:
        if path not in node.children:
            node.children[path] = DocTreeNode(scope_path=path)

    # Reparent existing children that are descendants of newly-created
    # parents.  E.g. if node.children already has "A / B" and we just
    # created "A", move "A / B" under "A".  This prevents "A / B" from
    # appearing as an orphan_child at the wrong tree depth.
    for parent_path in parent_paths:
        if parent_path in pre_existing:
            continue  # Don't reparent into pre-existing nodes
        parent_node = node.children[parent_path]
        to_move = [
            cp for cp in list(node.children.keys())
            if cp != parent_path
            and cp.startswith(parent_path + " / ")
        ]
        for cp in to_move:
            parent_node.children[cp] = node.children.pop(cp)

    # Split outline_items: keep items at this level, move descendants
    # into newly-created children only (skip pre-existing ones).
    kept: list[dict] = []
    for item in node.outline_items:
        item_path = item["path"]
        best_parent: str | None = None
        for pp in parent_paths:
            if item_path.startswith(pp + " / "):
                if best_parent is None or len(pp) > len(best_parent):
                    best_parent = pp
        if best_parent and best_parent not in pre_existing:
            node.children[best_parent].outline_items.append(item)
        else:
            kept.append(item)
    node.outline_items = kept

    # Reparent FIRST so children receive their leaf_content,
    # THEN recurse so deeper levels can be built from that content.
    node.reparent_leaf_content()
    for child in node.children.values():
        _build_outline_subtree(child)

