"""Coordinator for the document profile workflow."""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.services.document_agent.bootstrap import (
    aggregate_doc_stats,
    classify_page_kinds,
    probe_page_assets,
    probe_page_features,
)
from app.services.document_agent.manifest import (
    ProfileVerdict,
    DocumentProfile,
    PageAnatomyMap,
    TocResult,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.pdf_text import read_page_texts
from app.services.document_agent.persist import build_anatomy_map, persist_anatomy_map
from app.services.document_agent.coarse_profile import CoarseProfiler
from app.services.document_agent.registry import REGISTRY
from app.services.document_agent.state import ProfileBlackboard, ProfileState
from app.services.document_agent.structure.toc_anchoring import run_toc_anchoring
from app.services.document_agent import tools as _registered_tools  # noqa: F401
from app.services.document_agent.trace import ParseRunRecorder
from app.services.document_agent.validators import single_shard_plan


class ProfileCoordinator:
    def __init__(
        self,
        *,
        pdf_path: str,
        job_id: str,
        output_dir: str | None = None,
        db: Any | None = None,
        model: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.state = ProfileState.INIT
        self.blackboard = ProfileBlackboard()
        effective_settings = settings or {}
        if model:
            effective_settings["model"] = model
        self.ctx = ToolContext(
            pdf_path=pdf_path,
            job_id=job_id,
            blackboard=self.blackboard,
            trace=None,
            output_dir=output_dir,
            settings=effective_settings,
        )
        self.trace = ParseRunRecorder(job_id=job_id, db=db)
        self.ctx.trace = self.trace
        self.round_index = 0
        self._coarse_profile_cache: DocumentProfile | None = None

    def run_coarse(self) -> DocumentProfile:
        try:
            return self._run_coarse()
        except Exception as exc:
            self._record_failure(exc)
            raise

    def run_structural(self, *, skip_shard_plan: bool = False) -> PageAnatomyMap:
        try:
            return self._run_structural(skip_shard_plan=skip_shard_plan)
        except Exception as exc:
            self._record_failure(exc)
            raise

    def run_lightweight_anatomy(
        self, *, skip_shard_plan: bool = False
    ) -> PageAnatomyMap:
        try:
            return self._run_lightweight_anatomy(skip_shard_plan=skip_shard_plan)
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _run_coarse(self) -> DocumentProfile:
        self.state = ProfileState.RUNNING
        if not self.blackboard.page_features:
            self._run_bootstrap()
        profile = self._ensure_coarse_profile(actor="coarse_profile")
        self._run_text_scan()
        # Asset coarse probe is independent of TOC; run it before TOC so
        # PROFILE / debug Stage-0 share the same order as later anatomy/shard
        # consumers of ``page_features.has_asset``.
        self._ensure_asset_probe()
        if self.ctx.settings.get("stop_after_asset_probe"):
            # Debug Stage-0: bootstrap → coarse VLM → text scan → asset probe.
            return profile
        if self._toc_profile_enabled():
            self._ensure_toc_profile(strict=False)
        else:
            self._ensure_disabled_toc_placeholder()
        return profile

    def _run_structural(self, *, skip_shard_plan: bool = False) -> PageAnatomyMap:
        self.state = ProfileState.RUNNING
        if not self.blackboard.page_features:
            self._run_bootstrap()
        # Prefer assets before TOC so cold structural matches coarse order.
        # After ``run_coarse`` this is a no-op (assets_probed already set with
        # coarse header/footer margins).
        self._ensure_asset_probe()
        if self._toc_profile_enabled():
            self._ensure_toc_profile(strict=True)
        else:
            self._ensure_disabled_toc_placeholder()
        self._ensure_coarse_profile(actor="coarse_profile")
        if skip_shard_plan:
            # Page-memory oversized path never consumes shard_plan; only
            # build_anatomy_map's invariant needs a non-empty plan.
            self._apply_single_shard_placeholder()
        else:
            self._finalize_shard_plan()
        anatomy = build_anatomy_map(self.ctx)
        self._persist_ready_anatomy(anatomy)
        return anatomy

    def _run_lightweight_anatomy(
        self, *, skip_shard_plan: bool = False
    ) -> PageAnatomyMap:
        self.state = ProfileState.RUNNING
        if not self.blackboard.page_features:
            self._run_bootstrap()
        # Same relative order as coarse: assets before any TOC placeholder.
        # After ``run_coarse`` this is a no-op.
        self._ensure_asset_probe()
        if self.blackboard.toc_result is None:
            if self._toc_profile_enabled():
                self.blackboard.toc_result = TocResult(
                    method="none",
                    notes="TOC profiling not attempted",
                )
            else:
                self._ensure_disabled_toc_placeholder()
        if skip_shard_plan:
            # Page-based track processes pages individually via VLM and never
            # consumes the shard plan; only build_anatomy_map's invariant needs
            # it. Populate a single-shard placeholder to skip propose.shard_plan
            # (kept for chunk-track oversized MinerU sharding).
            self._apply_single_shard_placeholder()
        else:
            self._dispatch_anatomy_tool(tool_name="propose.shard_plan")
        anatomy = build_anatomy_map(self.ctx)
        self._persist_ready_anatomy(anatomy)
        return anatomy

    def _apply_single_shard_placeholder(self) -> None:
        self.blackboard.shard_plan = single_shard_plan(self.blackboard.page_count)

    def _dispatch_anatomy_tool(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any] | None = None,
    ) -> ToolResult:
        args = dict(tool_args or {})
        result = REGISTRY.dispatch(tool_name, self.ctx, args)
        self.trace.record_step(
            round_index=self.round_index,
            actor=f"anatomy:{tool_name}",
            action_type="anatomy",
            result=result,
            tool_name=tool_name,
            tool_args=args,
        )
        if result.status not in {"ok", "invalid"}:
            raise RuntimeError(result.error or f"{tool_name} failed")
        self.round_index += 1
        return result

    def _finalize_shard_plan(self) -> None:
        """Deterministic propose → validate → verdict.

        Cut logic stays inside ``propose.shard_plan``. Invalid validation aborts;
        do not disguise a single-shard rewrite as a successful propose.
        """
        if self.blackboard.shard_plan is None:
            self._dispatch_anatomy_tool(tool_name="propose.shard_plan")
        if not self.blackboard.validation_report:
            self._dispatch_anatomy_tool(tool_name="validate.anatomy_map")
        if (self.blackboard.validation_report or {}).get("valid") is True:
            self._dispatch_anatomy_tool(
                tool_name="verdict",
                tool_args={
                    "status": "success",
                    "rationale": "Validation succeeded; finishing profile run.",
                },
            )
            return

        self.blackboard.verdict = ProfileVerdict(
            status="abort",
            rationale="Shard plan validation failed.",
        )
        raise RuntimeError(
            f"profile aborted: {self.blackboard.verdict.rationale}"
        )

    def _persist_ready_anatomy(self, anatomy: PageAnatomyMap) -> None:
        persist_result = persist_anatomy_map(self.ctx, {})
        self.trace.record_step(
            round_index=self.round_index,
            actor="persist",
            action_type="persist",
            result=persist_result,
            tool_name="persist.anatomy_map",
            tool_args={},
        )
        self.state = ProfileState.READY
        self.trace.write_trace_artifact(
            self.ctx.output_dir,
            final_status="ready",
            summary=anatomy.trace_summary | self.trace.summary(),
        )
        self.trace.flush(
            final_status="ready",
            summary=anatomy.trace_summary | self.trace.summary(),
        )

    def _record_failure(self, exc: Exception) -> None:
        logger.error(f"[document_agent] profile failed: {exc}")
        self.state = ProfileState.FAILED
        self.trace.write_trace_artifact(
            self.ctx.output_dir,
            final_status="failed",
            summary={"error": str(exc)},
        )
        self.trace.flush(final_status="failed", summary={"error": str(exc)})

    def _run_bootstrap(self) -> None:
        for tool_name, handler in (
            ("probe.page_features", probe_page_features),
            ("classify.page_kinds", classify_page_kinds),
            ("aggregate.doc_stats", aggregate_doc_stats),
        ):
            result = handler(self.ctx, {})
            self.trace.record_step(
                round_index=self.round_index,
                actor=f"bootstrap:{tool_name}",
                action_type="bootstrap",
                result=result,
                tool_name=tool_name,
                tool_args={},
            )
            if result.status != "ok":
                raise RuntimeError(result.error or f"{tool_name} failed")
            self.round_index += 1

    def _ensure_asset_probe(self) -> None:
        if self.blackboard.global_signals.get("assets_probed"):
            return
        if not self.blackboard.page_features:
            raise RuntimeError("page_features missing; run text bootstrap first")
        for tool_name, handler in (
            ("probe.page_assets", probe_page_assets),
            ("aggregate.doc_stats", aggregate_doc_stats),
        ):
            result = handler(self.ctx, {})
            self.trace.record_step(
                round_index=self.round_index,
                actor=f"bootstrap:{tool_name}",
                action_type="bootstrap",
                result=result,
                tool_name=tool_name,
                tool_args={},
            )
            if result.status != "ok":
                raise RuntimeError(result.error or f"{tool_name} failed")
            self.round_index += 1

    def _toc_result_requires_strict_retry(self) -> bool:
        toc_result = self.blackboard.toc_result
        return bool(
            toc_result
            and toc_result.method == "none"
            and toc_result.failure_kind in {"confirm_failed", "degraded"}
        )

    def _toc_profile_enabled(self) -> bool:
        return bool(self.ctx.settings.get("toc_profile_enabled", True))

    def _run_text_scan(self) -> None:
        profile = self.blackboard.document_profile
        if profile is None:
            raise RuntimeError("document_profile missing; run coarse profile first")
        page_count = int(self.blackboard.page_count or 0)
        pages = list(range(1, page_count + 1))
        if not pages:
            self.blackboard.page_full_text_cache = {}
            return
        if profile.is_scanned:
            result = REGISTRY.dispatch("ocr.pages", self.ctx, {"pages": pages})
            self.trace.record_step(
                round_index=self.round_index,
                actor="scan:ocr.pages",
                action_type="scan",
                result=result,
                tool_name="ocr.pages",
                tool_args={"pages": pages},
            )
            if result.status != "ok":
                raise RuntimeError(result.error or "ocr.pages failed")
            self.round_index += 1
            return
        texts = read_page_texts(self.ctx.pdf_path, pages, timeout=300)
        self.blackboard.page_full_text_cache = texts
        self.trace.record_step(
            round_index=self.round_index,
            actor="scan:read_page_texts",
            action_type="scan",
            result=ToolResult(
                status="ok",
                payload={"page_count": len(texts)},
                output_summary={"page_count": len(texts)},
            ),
            tool_name="read_page_texts",
            tool_args={"pages": pages},
        )
        self.round_index += 1

    def _ensure_disabled_toc_placeholder(self) -> None:
        self.blackboard.toc_result = TocResult(
            method="none",
            notes="TOC profiling disabled by PDF_PROFILE_TOC_ENABLED",
        )
        self.blackboard.toc_hierarchies = None
        self._clear_toc_anchor_state()
        self.blackboard.global_signals["toc_profile_attempted"] = False

    def _ensure_toc_profile(self, *, strict: bool) -> None:
        should_run = self.blackboard.toc_result is None
        if strict and self._toc_result_requires_strict_retry():
            self.blackboard.toc_result = None
            self.blackboard.toc_hierarchies = None
            self._clear_toc_anchor_state()
            should_run = True

        if not should_run:
            return

        self.blackboard.global_signals["toc_profile_attempted"] = True
        try:
            self._run_toc_extraction_pipeline()
        except Exception as exc:
            logger.warning(
                "[document_agent] TOC profiling failed: {}",
                exc,
            )
            self.blackboard.toc_result = None
            self.blackboard.toc_hierarchies = None
            self._clear_toc_anchor_state()
            raise

        if self.blackboard.toc_result is None:
            self.blackboard.toc_result = TocResult(
                method="none",
                notes="TOC extraction completed without a result",
            )

    def _ensure_coarse_profile(self, *, actor: str) -> DocumentProfile:
        """Run one-shot coarse VLM classification once; cache the profile."""
        if self._coarse_profile_cache is not None:
            return self._coarse_profile_cache

        profile, result = CoarseProfiler(self.ctx).classify()
        self.blackboard.document_profile = profile
        self.trace.record_step(
            round_index=self.round_index,
            actor=actor,
            action_type="coarse_profile",
            result=result,
            tool_name=None,
            tool_args={},
        )
        self.round_index += 1
        self._coarse_profile_cache = profile
        return profile

    def _dispatch_profile_tool(self, *, tool_name: str, actor: str) -> ToolResult:
        result = REGISTRY.dispatch(tool_name, self.ctx, {})
        self.trace.record_step(
            round_index=self.round_index,
            actor=actor,
            action_type="toc",
            result=result,
            tool_name=tool_name,
            tool_args={},
        )
        if result.status not in {"ok", "invalid"}:
            raise RuntimeError(result.error or f"{tool_name} failed")
        self.round_index += 1
        return result

    def _clear_toc_anchor_state(self) -> None:
        self.blackboard.skeleton_anchor = None
        self.blackboard.skeleton_nodes = None
        self.blackboard.pending_skeleton_anchors = []

    def _run_toc_extraction_pipeline(self) -> None:
        for tool_name in (
            "find.toc_anchor_pages",
            "probe.outline",
            "extract.toc_with_boundaries",
        ):
            self._dispatch_profile_tool(
                tool_name=tool_name,
                actor=f"toc:{tool_name}",
            )
        if self.ctx.settings.get("skip_toc_anchoring"):
            # Debug Stage-1: stop after TOC extract.
            self._clear_toc_anchor_state()
            logger.info(
                "[document_agent] skip_toc_anchoring=True; "
                "leaving calibration to a later stage"
            )
            return
        run_toc_anchoring(self.ctx)

