"""Per-document navigation for agentic retrieval.

Collector Agent architecture
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The navigation loop uses an observe-act Collector Agent model where each step
produces one main action and optional collection side effects:

1. **collect**: paths to add to the evidence collection
2. **action**: EXPAND/BACK/SEARCH_IMAGES/SEARCH_TABLES/FINISH

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
    DecisionTraceStep,
    DocTreeNode,
    NavigateStepResult,
    ToolResult,
)
from shared.services.retrieval.agentic.navigation.selection_hydration import (
    hydrate_path_selections_into_node,
)
from shared.services.retrieval.agentic.navigation.path_ledger import PathLedger
from shared.services.retrieval.agentic.navigation.state import NavigationState
from shared.services.retrieval.agentic.prompts import (
    QUERY_INTENT_PROMPT,
    parse_query_intent_response,
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
        query_intent = await self._classify_query_intent()
        for doc in self._state.selected_docs:
            if self._state.elapsed_ms >= self._config.latency_budget_ms:
                logger.info("  agentic: latency budget hit during Phase 2, stopping")
                break
            await self._navigate_document(doc, query_intent=query_intent)

    async def _navigate_document(
        self,
        doc: CandidateDoc,
        *,
        query_intent: str,
    ) -> None:
        job_result_id = self._state.doc_job_map.get(doc.document_id, "")
        if not job_result_id:
            logger.info(f"  agentic: skipping doc {doc.document_id} — no job_result_id")
            self._state.ever_explored_doc_ids.add(doc.document_id)
            return

        doc_name = doc.source_file_name or self._state.doc_id_to_name.get(doc.document_id, "")
        root = DocTreeNode(scope_path=None)
        doc_pending_assets: list[dict[str, Any]] = []
        from shared.services.retrieval.agentic.navigation.section_tree import (
            load_document_section_rows,
        )
        section_rows = await load_document_section_rows(
            self._db,
            document_id=doc.document_id,
            job_result_id=job_result_id,
        )

        # Phase 2A: Collector Agent navigation (summary-only, no content hydration)
        doc_pending_assets, collected_paths = await self._navigate_collector(
            doc=doc,
            root=root,
            doc_name=doc_name,
            job_result_id=job_result_id,
            section_rows=section_rows,
            query_intent=query_intent,
        )

        # Phase 2B: Batch hydrate all collected paths
        if collected_paths:
            await self._hydrate_collected(
                doc=doc,
                root=root,
                job_result_id=job_result_id,
                collected_paths=collected_paths,
                section_rows=section_rows,
            )

        # Phase 2C: Reconcile assets into hydrated tree
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
        section_rows: list,
        query_intent: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Collector Agent navigation loop.

        Returns (doc_pending_assets, collected_paths).
        """
        doc_exclude: set[str] = set()
        doc_discovery_hints = self._discovery_by_doc.get(doc.document_id, [])
        doc_pending_assets: list[dict[str, Any]] = []
        nav_state = NavigationState(
            document_id=doc.document_id,
            document_name=doc_name,
            job_result_id=job_result_id,
        )
        # Context from SEARCH tools — injected into next navigate prompt
        search_context: str = ""
        prior_tool_result: dict[str, Any] | None = None

        exit_reason = "unknown"
        budget_failure: dict[str, Any] | None = None

        while nav_state.step_count < self._config.max_nav_steps:
            if self._state.elapsed_ms >= self._config.latency_budget_ms:
                exit_reason = "latency"
                break
            has_tool_context = bool(prior_tool_result or search_context)
            if (
                self._state.ledger
                and self._state.ledger.status("planning") == "EXHAUSTED"
                and not has_tool_context
            ):
                logger.info("  agentic: planning budget exhausted, ending navigation for current doc")
                exit_reason = "budget"
                break

            nav_state.step_count += 1
            before_scope = nav_state.current_scope
            expanded_before = set(nav_state.expanded_scopes)
            rejected_before = set(nav_state.rejected_paths)
            rejected_collect_before = set(nav_state.rejected_collect_paths)
            collected_before_count = len(nav_state.collected_paths)

            doc_llm_fn = self._llm_budget.for_document(
                cast(LLMFn, self._llm_fn),
                doc_id=doc.document_id,
                step=nav_state.step_count,
                allow_overdraft=has_tool_context,
                overdraft_reason=(
                    "report_tool_result_to_main_agent"
                    if has_tool_context else ""
                ),
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
                    scope_path=nav_state.current_scope,
                    exclude_paths=doc_exclude,
                    budget_snapshot=self._state.ledger.snapshot() if self._state.ledger else None,
                    nav_trace=nav_state.nav_trace if nav_state.nav_trace else None,
                    collected_paths=nav_state.collected_paths,
                    expanded_scopes=nav_state.expanded_scopes,
                    rejected_paths=nav_state.rejected_paths,
                    rejected_collect_paths=nav_state.rejected_collect_paths,
                    disabled_asset_types=nav_state.blocked_asset_types_for_scope(
                        nav_state.current_scope
                    ),
                    discovery_hints=doc_discovery_hints,
                    section_rows=section_rows,
                    query_intent=query_intent,
                    search_context=search_context,
                    prior_tool_result=prior_tool_result,
                )
            except BudgetExceeded as exc:
                budget_failure = getattr(exc, "details", {}) or {}
                logger.info(
                    "  agentic: planning budget exhausted during navigation "
                    f"details={budget_failure}"
                )
                if self._trace_enabled:
                    self._trace.record_budget_stop("planning_exhausted")
                exit_reason = "budget"
                break
            self._state.step_count += 1

            # Clear previous tool context (consumed by this step's prompt)
            search_context = ""
            prior_tool_result = None

            # ── Execute asset tools (SEARCH) ─────────────────────────────
            asset_tool_result = await self._execute_asset_tools(
                doc=doc,
                job_result_id=job_result_id,
                scope=nav_state.current_scope,
                nav_result=nav_result,
                pending_assets=doc_pending_assets,
                parent_step_index=len(self._decision_steps),
            )
            search_context = asset_tool_result.get("context", "")
            prior_tool_result = asset_tool_result.get("summary")
            if (
                prior_tool_result is not None
                and self._should_block_asset_search(
                    prior_tool_result, nav_state.current_scope,
                )
            ):
                nav_state.block_asset_search(
                    nav_state.current_scope,
                    str(prior_tool_result.get("asset_type") or ""),
                )

            # Merge outline + confidence into root tree
            _merge_step_node(root, nav_result.node)

            requested_collects = list(nav_result.collect)
            collect_reconcile = self._reconcile_collects_after_tool(
                nav_result=nav_result,
                asset_tool_result=asset_tool_result,
            )
            rejected_collects = collect_reconcile["rejected_collects"]
            if rejected_collects:
                nav_result.collect = collect_reconcile["accepted_collects"]
                for path in rejected_collects:
                    nav_state.mark_rejected_collect(path)
                    doc_exclude.add(path)
                logger.info(
                    "  agentic: tool reconciliation rejected collects: "
                    f"{rejected_collects}"
                )

            # ── Process COLLECT ──────────────────────────────────────────
            collected_in_step: list[str] = []
            for coll_item in nav_result.collect:
                path = coll_item["path"]
                nav_state.add_collected(
                    coll_item,
                    step=nav_state.step_count,
                    scope_context=nav_state.current_scope,
                )
                collected_in_step.append(path)
                # Outline collections should NOT exclude children — the intent
                # is "see structure, then drill deeper for full content".
                if coll_item.get("hydrate_mode") != "outline":
                    doc_exclude.add(path)

            # ── Process navigation action ────────────────────────────────
            should_break = False
            if (
                nav_result.action == "EXPAND"
                and nav_result.result_status == "ok"
                and nav_result.drill_into
            ):
                drill_path = nav_result.drill_into
                # Create child node in tree for the drill target
                target_parent = _find_target_node(root, drill_path)
                target_parent.children.setdefault(drill_path, DocTreeNode(scope_path=drill_path))
                nav_state.mark_expanded(drill_path)
                nav_state.current_scope = drill_path

            elif nav_result.action == "BACK" and nav_result.result_status == "ok":
                if nav_state.current_scope is None:
                    logger.info("  agentic: BACK at root scope, staying at root")
                    nav_result.result_status = "invalid_back"
                    nav_result.result_note = "already_at_root"
                else:
                    back_target = nav_result.back_to  # None = root
                    if PathLedger.valid_back_target(nav_state.current_scope, back_target):
                        nav_state.mark_rejected_if_unproductive(nav_state.current_scope)
                        nav_state.current_scope = back_target
                    else:
                        logger.warning(
                            f"  agentic: invalid back_to='{back_target}' "
                            f"from scope='{nav_state.current_scope}'"
                        )
                        nav_result.result_status = "invalid_back"
                        nav_result.result_note = f"invalid_back_target: {back_target}"

            elif nav_result.action == "ERROR":
                logger.warning(
                    f"  agentic: navigation ERROR for doc={doc.document_id}: "
                    f"{nav_result.error_reason or nav_result.reason}"
                )
                exit_reason = "error"
                should_break = True

            elif nav_result.action == "FINISH" and nav_result.result_status == "ok":
                exit_reason = "llm_finish"
                should_break = True

            # ── Build trace entry ────────────────────────────────────────
            state_delta = nav_state.snapshot_delta(
                before_scope=before_scope,
                expanded_before=expanded_before,
                rejected_before=rejected_before,
                rejected_collect_before=rejected_collect_before,
                collected_before_count=collected_before_count,
            )
            trace_entry: dict[str, Any] = {
                "step": nav_state.step_count,
                "scope": before_scope or "root",
                "action": nav_result.action,
                "drill_into": nav_result.drill_into,
                "back_to": nav_result.back_to,
                "collected": collected_in_step,
                "tools_used": nav_result.tools,
                "reason": nav_result.reason,
                "result_status": nav_result.result_status,
                "state_delta": state_delta,
            }
            if rejected_collects:
                trace_entry["requested_collects"] = [
                    item.get("path", "")
                    for item in requested_collects
                    if item.get("path")
                ]
                trace_entry["rejected_collects"] = rejected_collects
                trace_entry["tool_reconciliation"] = collect_reconcile["reason"]
            # Record tool usage & results so future steps can see search history
            if prior_tool_result:
                trace_entry["tool_results"] = prior_tool_result
            nav_state.nav_trace.append(trace_entry)

            # ── Record decision step ─────────────────────────────────────
            main_step_index = self._record_navigation_step(
                doc=doc,
                scope=before_scope,
                step_num=nav_state.step_count,
                nav_result=nav_result,
                collected_in_step=collected_in_step,
                asset_summary=asset_tool_result.get("summary"),
                rejected_collects=rejected_collects,
                state_delta=state_delta,
            )
            asset_trace = asset_tool_result.get("asset_trace")
            if asset_trace:
                asset_trace["parent_step_index"] = main_step_index
                self._append_decision_trace_step(DecisionTraceStep(**asset_trace))

            if should_break:
                break
        else:
            # while loop exhausted — max_nav_steps reached
            exit_reason = "max_steps"

        # Hard guard: if navigation is forcefully interrupted and collected
        # nothing, collect visible leaf children under the last explored
        # scope. Voluntary FINISH/BACK with empty collection is respected.
        #   budget    – planning pool EXHAUSTED (pre-check or exception)
        #   latency   – elapsed time exceeded latency budget
        #   max_steps – navigation step count limit reached
        #   error     – unexpected exception during navigate_step
        forced_exits = ("budget", "latency", "max_steps", "error")
        guard_triggered = False
        if not nav_state.collected_paths and nav_state.step_count > 0 and exit_reason in forced_exits:
            guard_triggered = True
            guard_scope = nav_state.current_scope
            logger.info(
                f"  agentic: forced exit ({exit_reason}) with 0 collected paths, "
                f"auto-collecting leaves under scope={guard_scope or 'root'}"
            )
            from shared.services.retrieval.agentic.navigation.section_tree import (
                load_child_sections,
            )
            guard_items = await load_child_sections(
                self._db,
                doc.document_id,
                job_result_id,
                guard_scope,
                section_rows=section_rows,
            )
            for item in guard_items:
                if not item.get("show_summary", True):
                    continue
                if item.get("is_leaf"):
                    path = item["path"]
                    nav_state.collected_paths.append({
                        "path": path,
                        "confidence": 0.4,
                        "hydrate_mode": "chunks",
                        "collected_at_step": nav_state.step_count,
                        "scope_context": guard_scope or "root",
                        "guard_reason": f"forced_exit_{exit_reason}",
                    })

            self._append_decision_trace_step(DecisionTraceStep(
                step_index=len(self._decision_steps),
                agent="system",
                phase="system_guard",
                document_id=doc.document_id,
                document=doc.source_file_name or "",
                scope=guard_scope or "root",
                observation={
                    "exit_reason": exit_reason,
                    "collected_count": 0,
                },
                decision={
                    "action": "auto_collect_visible_leaves",
                    "args": {"scope": guard_scope or "root"},
                    "reason": "hard navigation constraint stopped the loop before evidence collection",
                },
                result={
                    "status": "guard_auto_collect",
                    "collected": [
                        {
                            "path": p["path"],
                            "confidence": p.get("confidence", 0.0),
                        }
                        for p in nav_state.collected_paths
                    ],
                    "note": f"forced_exit_{exit_reason}",
                },
                budget=self._state.ledger.snapshot() if self._state.ledger else None,
                elapsed_ms=self._state.elapsed_ms,
            ))

        # ── Navigate summary — record exit reason and final state ─────
        doc_name = doc.source_file_name or self._state.doc_id_to_name.get(doc.document_id, "")
        self._append_decision_trace_step(DecisionTraceStep(
            step_index=len(self._decision_steps),
            agent="navigator",
            phase="navigate_summary",
            document_id=doc.document_id,
            document=doc_name,
            scope=nav_state.current_scope or "root",
            observation={
                "total_steps": nav_state.step_count,
                "collected_count": len(nav_state.collected_paths),
                "guard_triggered": guard_triggered,
                "query_intent": query_intent,
                "budget_failure": budget_failure,
            },
            decision={
                "action": "summarize_navigation",
                "args": {},
                "reason": f"Navigation ended with exit_reason={exit_reason}",
            },
            result={
                "status": exit_reason,
                "final_scope": nav_state.current_scope or "root",
                "collected": [
                    {
                        "path": p.get("path", ""),
                        "confidence": p.get("confidence", 0.0),
                    }
                    for p in nav_state.collected_paths
                ],
            },
            budget=self._state.ledger.snapshot() if self._state.ledger else None,
            elapsed_ms=self._state.elapsed_ms,
        ))

        return doc_pending_assets, nav_state.collected_paths

    async def _classify_query_intent(
        self,
    ) -> str:
        """Classify query intent as advisory state. Fail-open to UNKNOWN."""
        if self._llm_fn is None:
            return "UNKNOWN"
        prompt = QUERY_INTENT_PROMPT.format(
            query=self._query,
        )
        try:
            response = await self._llm_budget.call(
                cast(LLMFn, self._llm_fn),
                prompt,
                pool="planning",
                priority="low",
            )
        except Exception as exc:
            logger.info(
                f"  agentic: query intent classifier failed-open: {exc}"
            )
            return "UNKNOWN"
        return parse_query_intent_response(response)

    async def _hydrate_collected(
        self,
        *,
        doc: CandidateDoc,
        root: DocTreeNode,
        job_result_id: str,
        collected_paths: list[dict[str, Any]],
        section_rows: list,
    ) -> None:
        """Batch-hydrate all collected paths after navigation completes."""
        if not collected_paths:
            return

        # Deduplicate: keep the most complete evidence mode per path.  A later
        # full collect is an upgrade over an earlier outline collect even when
        # the outline confidence was higher.
        deduped: dict[str, dict[str, Any]] = {}
        for item in collected_paths:
            path = item["path"]
            if path not in deduped or _collect_rank(item) > _collect_rank(deduped[path]):
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
                    section_rows=section_rows,
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
        parent_step_index: int,
    ) -> dict[str, Any]:
        """Execute SEARCH_* and return the observation for the next loop."""
        empty_result: dict[str, Any] = {
            "context": "",
            "summary": None,
            "asset_trace": None,
        }
        if not nav_result.tools:
            return empty_result

        if not nav_result.search_assets_params:
            return empty_result

        params = nav_result.search_assets_params
        search_query = params["query"]
        asset_type = params["asset_type"]
        scope_paths = params.get("scope_paths")
        tool_name = "SEARCH_IMAGES" if asset_type == "image" else "SEARCH_TABLES"
        budget_before = self._state.ledger.snapshot() if self._state.ledger else None

        search_llm_fn = self._llm_budget.for_document(
            cast(LLMFn, self._llm_fn),
            doc_id=doc.document_id,
            step=self._state.step_count,
            allow_overdraft=True,
            overdraft_reason=f"{tool_name}_asset_inspector",
        ) if self._llm_fn else None
        if search_llm_fn is None:
            summary = {
                "tool": tool_name,
                "asset_type": asset_type,
                "query": search_query,
                "matched": 0,
                "status": "unavailable",
                "scope_paths": scope_paths if scope_paths is not None else ([scope] if scope else []),
                "matched_paths": [],
                "sub_agent_assessment": "LLM unavailable for asset inspection",
            }
            return {
                "context": self._format_asset_context(
                    tool_name,
                    asset_type,
                    search_query,
                    [],
                    status="unavailable",
                ),
                "summary": summary,
                "asset_trace": self._build_asset_trace_payload(
                    doc=doc,
                    scope=scope,
                    parent_step_index=parent_step_index,
                    asset_type=asset_type,
                    query=search_query,
                    candidates=[],
                    result={"status": "unavailable", "verdicts": [], "matched_assets": []},
                ),
            }

        vlm_fn: LLMFn | None = None
        if asset_type == "image":
            from shared.services.retrieval.llm_adapter import create_retrieval_vlm_fn
            raw_vlm_fn = create_retrieval_vlm_fn()
            if raw_vlm_fn is not None:
                vlm_fn = self._llm_budget.for_document(
                    raw_vlm_fn,
                    doc_id=doc.document_id,
                    step=self._state.step_count,
                    allow_overdraft=True,
                    overdraft_reason=f"{tool_name}_vlm_asset_inspector",
                )

        try:
            asset_result = await tools.search_assets_step(
                self._db,
                document_id=doc.document_id,
                job_result_id=job_result_id,
                scope_path=scope_paths if scope_paths is not None else scope,
                asset_type=asset_type,
                query=search_query,
                llm_fn=search_llm_fn,
                vlm_fn=vlm_fn,
            )
        except BudgetExceeded as exc:
            failure = getattr(exc, "details", {}) or {}
            logger.info(
                f"  agentic: {tool_name} skipped — planning budget exhausted "
                f"details={failure}"
            )
            asset_result = {
                "status": "budget_exceeded",
                "status_detail": "budget_reserve_failed",
                "budget_failure": failure,
                "matched_assets": [],
                "verdicts": [],
                "candidate_count": 0,
            }

        budget_after = self._state.ledger.snapshot() if self._state.ledger else None
        budget_delta = _budget_delta(budget_before, budget_after)
        matched_assets = asset_result.get("matched_assets") or []
        verdicts = asset_result.get("verdicts") or []
        candidate_count = int(asset_result.get("candidate_count") or 0)
        if matched_assets:
            pending_assets.extend(matched_assets)

        summary = {
            "tool": tool_name,
            "asset_type": asset_type,
            "query": search_query,
            "matched": len(matched_assets),
            "candidate_count": candidate_count,
            "status": asset_result.get("status", "empty"),
            "status_detail": asset_result.get("status_detail", ""),
            "budget": {
                "before": _compact_budget_snapshot(budget_before),
                "after": _compact_budget_snapshot(budget_after),
                "delta": budget_delta,
            },
            "scope_paths": scope_paths if scope_paths is not None else ([scope] if scope else []),
            "matched_paths": [
                asset.get("file_path", "")
                for asset in matched_assets
                if asset.get("file_path")
            ],
            "matched_owner_paths": [
                asset.get("owner_section_path") or asset.get("section_path") or ""
                for asset in matched_assets
                if asset.get("owner_section_path") or asset.get("section_path")
            ],
            "sub_agent_assessment": (
                f"asset inspector matched {len(matched_assets)} "
                f"of {candidate_count} {asset_type} candidates "
                f"(status={asset_result.get('status', 'empty')})"
            ),
        }
        logger.info(
            f"  agentic step {self._state.step_count}: {tool_name} "
            f'doc="{doc.source_file_name}" scope={scope or "root"} '
            f'search_scope={scope_paths if scope_paths is not None else scope or "root"} '
            f'query="{search_query}" matched={len(matched_assets)}'
        )
        return {
            "context": self._format_asset_context(
                tool_name,
                asset_type,
                search_query,
                matched_assets,
                status=str(asset_result.get("status", "empty")),
                status_detail=str(asset_result.get("status_detail", "")),
            ),
            "summary": summary,
            "matched_assets": matched_assets,
            "candidate_count": candidate_count,
            "asset_trace": self._build_asset_trace_payload(
                doc=doc,
                scope=scope,
                parent_step_index=parent_step_index,
                asset_type=asset_type,
                query=search_query,
                candidates=verdicts,
                result=asset_result,
                budget_before=budget_before,
                budget_after=budget_after,
                budget_delta=budget_delta,
            ),
            }

    @staticmethod
    def _should_block_asset_search(
        summary: dict[str, Any] | None,
        current_scope: str | None,
    ) -> bool:
        if not summary:
            return False
        if int(summary.get("matched") or 0) > 0:
            return False
        if not _tool_searched_current_scope(summary, current_scope):
            return False
        return str(summary.get("status") or "").lower() in {
            "empty",
            "fallback_empty",
            "unavailable",
            "error",
            "budget_exceeded",
        }

    @staticmethod
    def _reconcile_collects_after_tool(
        *,
        nav_result: NavigateStepResult,
        asset_tool_result: dict[str, Any],
    ) -> dict[str, Any]:
        accepted_collects = list(nav_result.collect)
        empty = {
            "accepted_collects": accepted_collects,
            "rejected_collects": [],
            "reason": "",
        }
        if not accepted_collects or not nav_result.tools:
            return empty
        summary = asset_tool_result.get("summary")
        if not isinstance(summary, dict):
            return empty
        tool_name = str(summary.get("tool") or "")
        if tool_name not in {"SEARCH_IMAGES", "SEARCH_TABLES"}:
            return empty
        status = str(summary.get("status") or "").lower()
        if status not in {"empty", "fallback_empty", "matched", "fallback_matched"}:
            return empty

        matched_assets = asset_tool_result.get("matched_assets") or []
        matched_owner_paths = [
            str(asset.get("owner_section_path") or asset.get("section_path") or "")
            for asset in matched_assets
            if asset.get("owner_section_path") or asset.get("section_path")
        ]
        still_accepted: list[dict[str, Any]] = []
        rejected_paths: list[str] = []
        for item in accepted_collects:
            path = str(item.get("path") or "")
            if not path:
                continue
            has_matching_asset = any(
                PathLedger.is_same_or_descendant(owner_path, path)
                for owner_path in matched_owner_paths
            )
            if has_matching_asset:
                still_accepted.append(item)
            else:
                rejected_paths.append(path)

        if not rejected_paths:
            return empty
        reason = (
            f"{tool_name} returned no valid matching assets under rejected "
            f"collect paths; status={status}, matched={len(matched_assets)}"
        )
        return {
            "accepted_collects": still_accepted,
            "rejected_collects": rejected_paths,
            "reason": reason,
        }

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

    @staticmethod
    def _format_asset_context(
        tool_name: str,
        asset_type: str,
        search_query: str,
        matched_assets: list[dict[str, Any]],
        *,
        status: str = "empty",
        status_detail: str = "",
    ) -> str:
        if not matched_assets:
            detail = f" Status detail: {status_detail}." if status_detail else ""
            return (
                f"=== {tool_name} Results ===\n"
                f"No matching {asset_type}s found for \"{search_query}\" "
                f"(status={status}).{detail}\n"
                f"=== End {tool_name} Results ==="
            )
        lines = [
            f"=== {tool_name} Results ===",
            f'Found {len(matched_assets)} matching {asset_type}s for "{search_query}".',
            "Matched assets are available as asset evidence.",
        ]
        for i, asset in enumerate(matched_assets):
            file_path = asset.get("file_path", "")
            lines.append(f"  {i + 1}. {file_path}")
        owner_paths = _unique_asset_owner_paths(matched_assets)
        if owner_paths:
            lines.append("Owner sections with matching assets:")
            for owner_path in owner_paths:
                lines.append(f'  - "{owner_path}"')
        lines.append(
            "Use these asset results and owner sections to decide collect, "
            "finish, back, or further navigation."
        )
        lines.append(f"=== End {tool_name} Results ===")
        return "\n".join(lines)

    def _build_asset_trace_payload(
        self,
        *,
        doc: CandidateDoc,
        scope: str | None,
        parent_step_index: int,
        asset_type: str,
        query: str,
        candidates: list[dict[str, Any]],
        result: dict[str, Any],
        budget_before: dict[str, Any] | None = None,
        budget_after: dict[str, Any] | None = None,
        budget_delta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        matched_assets = result.get("matched_assets") or []
        matched = [
            {
                "chunk_id": asset.get("chunk_id", ""),
                "file_path": asset.get("file_path", ""),
                "section_path": asset.get("owner_section_path")
                or asset.get("section_path", ""),
            }
            for asset in matched_assets
        ]
        return {
            "step_index": len(self._decision_steps),
            "agent": "asset_inspector",
            "parent_step_index": parent_step_index,
            "phase": "asset_inspect",
            "document_id": doc.document_id,
            "document": doc.source_file_name or "",
            "scope": scope or "root",
            "observation": {
                "asset_type": asset_type,
                "query": query,
                "candidates": candidates,
            },
            "decision": {
                "action": "inspect_assets",
                "args": {"asset_type": asset_type, "query": query},
                "reason": "judged each candidate against the requested evidence",
            },
            "result": {
                "status": result.get("status", "empty"),
                "status_detail": result.get("status_detail", ""),
                "verdicts": result.get("verdicts") or [],
                "matched": matched,
                "budget_failure": result.get("budget_failure"),
                "budget": {
                    "before": _compact_budget_snapshot(budget_before),
                    "after": _compact_budget_snapshot(budget_after),
                    "delta": budget_delta or {},
                },
            },
            "budget": self._state.ledger.snapshot() if self._state.ledger else None,
            "elapsed_ms": self._state.elapsed_ms,
        }

    def _append_decision_trace_step(self, step: DecisionTraceStep) -> int:
        step.step_index = len(self._decision_steps)
        self._decision_steps.append(step.to_dict())
        if self._trace_enabled:
            self._trace.record_decision_trace_step(step)
        return step.step_index

    def _record_navigation_step(
        self,
        *,
        doc: CandidateDoc,
        scope: str | None,
        step_num: int,
        nav_result: NavigateStepResult,
        collected_in_step: list[str],
        asset_summary: dict[str, Any] | None = None,
        rejected_collects: list[str] | None = None,
        state_delta: dict[str, Any] | None = None,
    ) -> int:
        action = nav_result.action
        reason = nav_result.reason
        drill_into = nav_result.drill_into
        doc_name = doc.source_file_name or self._state.doc_id_to_name.get(doc.document_id, "")
        collected = [
            {
                "path": item.get("path", ""),
                "confidence": item.get("confidence", 0.0),
                "hydrate_mode": item.get("hydrate_mode", "chunks"),
            }
            for item in nav_result.collect
        ]
        decision_args: dict[str, Any] = {}
        if drill_into:
            decision_args["target"] = drill_into
        if action == "BACK":
            decision_args["target"] = nav_result.back_to
        if nav_result.search_assets_params:
            decision_args["query"] = nav_result.search_assets_params.get("query", "")
            decision_args["asset_type"] = nav_result.search_assets_params.get("asset_type", "")
        projected_scope = scope or "root"
        if action == "EXPAND" and drill_into:
            projected_scope = drill_into
        elif action == "BACK" and nav_result.result_status == "ok":
            projected_scope = nav_result.back_to or "root"

        result_payload: dict[str, Any] = {
            "status": nav_result.result_status,
            "collected": collected,
            "new_scope": projected_scope,
            "note": nav_result.result_note,
        }
        if state_delta is not None:
            result_payload["state_delta"] = state_delta
        if rejected_collects:
            result_payload["rejected_collects"] = rejected_collects
        if nav_result.error_reason:
            result_payload["error"] = nav_result.error_reason
        if asset_summary:
            result_payload["matched_assets"] = asset_summary.get("matched", 0)
            result_payload["tool_status"] = asset_summary.get("status")
            result_payload["tool_budget"] = asset_summary.get("budget")
            result_payload["sub_agent_assessment"] = asset_summary.get(
                "sub_agent_assessment"
            )

        trace_step = DecisionTraceStep(
            step_index=len(self._decision_steps),
            agent="navigator",
            phase="navigate",
            document=doc_name,
            document_id=doc.document_id,
            scope=scope or "root",
            observation=nav_result.observation,
            decision={
                "action": action,
                "args": decision_args,
                "reason": reason,
            },
            result=result_payload,
            budget=self._state.ledger.snapshot() if self._state.ledger else None,
            elapsed_ms=self._state.elapsed_ms,
        )
        step_index = self._append_decision_trace_step(trace_step)

        status_tag = (
            f" status={nav_result.result_status}"
            if nav_result.result_status != "ok"
            else ""
        )
        scope_log = scope or "root"
        logger.info(
            f"  agentic step {self._state.step_count}: navigate_step "
            f'doc="{doc.source_file_name}" scope={scope_log} '
            f"step={step_num} action={action} tools={nav_result.tools} "
            f'reason="{reason[:80]}" '
            f"collected={len(collected_in_step)} "
            f"drill_into={drill_into} "
            f"outline={len(nav_result.node.outline_items)}"
            f"{status_tag}"
        )
        return step_index


def _tool_searched_current_scope(
    summary: dict[str, Any],
    current_scope: str | None,
) -> bool:
    current = PathLedger.normalize(current_scope) or "root"
    scope_paths = summary.get("scope_paths")
    if not isinstance(scope_paths, list) or not scope_paths:
        return current == "root"
    searched = [
        PathLedger.normalize(str(path or ""))
        for path in scope_paths
        if PathLedger.normalize(str(path or ""))
    ]
    if current == "root":
        return not searched
    return searched == [current]


def _compact_budget_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    planning = snapshot.get("planning")
    if not isinstance(planning, dict):
        return {}
    compact = {
        "status": planning.get("status"),
        "used_pct": planning.get("used_pct"),
        "remaining": planning.get("remaining"),
        "capacity": planning.get("capacity"),
        "overdraft": planning.get("overdraft", 0),
    }
    overdraft_events = snapshot.get("overdraft_events")
    if isinstance(overdraft_events, list) and overdraft_events:
        compact["overdraft_events"] = overdraft_events[-3:]
    return compact


def _budget_delta(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    before_planning = (
        before.get("planning") if isinstance(before, dict) else None
    )
    after_planning = (
        after.get("planning") if isinstance(after, dict) else None
    )
    if not isinstance(before_planning, dict) or not isinstance(after_planning, dict):
        return {}
    return {
        "used": int(after_planning.get("used") or 0)
        - int(before_planning.get("used") or 0),
        "used_pct": int(after_planning.get("used_pct") or 0)
        - int(before_planning.get("used_pct") or 0),
        "remaining": int(after_planning.get("remaining") or 0)
        - int(before_planning.get("remaining") or 0),
        "overdraft": int(after_planning.get("overdraft") or 0)
        - int(before_planning.get("overdraft") or 0),
    }


def _unique_asset_owner_paths(matched_assets: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    owner_paths: list[str] = []
    for asset in matched_assets:
        owner_path = str(asset.get("owner_section_path") or asset.get("section_path") or "")
        if not owner_path or owner_path in seen:
            continue
        seen.add(owner_path)
        owner_paths.append(owner_path)
    return owner_paths


def _find_target_node(node: DocTreeNode, path: str) -> DocTreeNode:
    """Walk the tree to find the deepest existing node that owns *path*.

    Only recurse when *path* is a true descendant of a child (prefix match).
    An exact match means the item belongs to the section itself, which is
    managed by the *parent* node — the renderer already handles the case
    where a path appears in both ``children`` and ``leaf_content``.
    """
    for child_path, child in node.children.items():
        if PathLedger.is_ancestor(child_path, path):
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


def _collect_rank(item: dict[str, Any]) -> tuple[int, float]:
    mode = str(item.get("hydrate_mode") or "chunks")
    mode_rank = 0 if mode == "outline" else 1
    confidence = float(item.get("confidence") or 0.0)
    return (mode_rank, confidence)


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
        if PathLedger.is_ancestor(child_path, path):
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
            if other != path and PathLedger.is_ancestor(path, other):
                parent_paths.add(path)
                break

    if not parent_paths:
        return  # All items are leaves — nothing to nest.

    # KEY FIX: only keep top-level parents.  If "A / B" and "A / B / C"
    # are both parents, only create "A / B" as a child NOW.  "A / B / C"
    # will be created when the function recurses into "A / B".
    parent_paths = {
        pp for pp in parent_paths
        if not any(pp != other and PathLedger.is_ancestor(other, pp) for other in parent_paths)
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
            and PathLedger.is_ancestor(parent_path, cp)
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
            if PathLedger.is_ancestor(pp, item_path):
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
