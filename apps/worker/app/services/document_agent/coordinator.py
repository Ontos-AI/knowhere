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
    DocumentProfile,
    PageAnatomyMap,
    TocResult,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.pdf_text import page_content, read_page_text_bands
from app.services.document_agent.persist import build_anatomy_map, persist_anatomy_map
from app.services.document_agent.coarse_profile import CoarseProfiler
from app.services.document_agent.slide_structure import run_slide_structure_route
from app.services.document_agent.registry import REGISTRY
from app.services.document_agent.state import ProfileBlackboard, ProfileState
from app.services.document_agent.structure.toc_anchoring import run_toc_anchoring
from app.services.document_agent import tools as _registered_tools  # noqa: F401
from app.services.document_agent.trace import ParseRunRecorder
from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory
from app.services.document_parser.support.stage_profiler import stage_timer


class ProfileCoordinator:
    def __init__(
        self,
        *,
        pdf_path: str,
        job_id: str,
        output_dir: str | None = None,
        db: Any | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.state = ProfileState.INIT
        self.blackboard = ProfileBlackboard()
        self.ctx = ToolContext(
            pdf_path=pdf_path,
            job_id=job_id,
            blackboard=self.blackboard,
            trace=None,
            output_dir=output_dir,
            settings=settings or {},
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

    def run_anatomy(self) -> PageAnatomyMap:
        """Build and persist the anatomy map once coarse profiling and TOC are done."""
        try:
            self.state = ProfileState.RUNNING
            anatomy = build_anatomy_map(self.ctx)
            self._persist_ready_anatomy()
            return anatomy
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _run_coarse(self) -> DocumentProfile:
        self.state = ProfileState.RUNNING
        if not self.blackboard.page_features:
            self._run_bootstrap()
        with stage_timer(
            "document_profile.classification",
            job_id=self.ctx.job_id,
            page_count=self.blackboard.page_count,
        ):
            profile: DocumentProfile = self._ensure_coarse_profile(actor="coarse_profile")
        with stage_timer(
            "document_profile.text_scan",
            job_id=self.ctx.job_id,
            page_count=self.blackboard.page_count,
        ):
            self._run_text_scan()
        if (
            self.ctx.settings.get("stop_after_native_baseline")
            and not profile.is_scanned
            and any(
                page_content(bands).strip()
                for bands in self.blackboard.page_full_text_cache.values()
            )
        ):
            # Progressive native pages already have their complete band text.
            # Asset flags remain unknown until the background structure stage.
            return profile
        # Asset coarse probe is independent of TOC; run it before TOC so
        # PROFILE / debug Stage-0 share the same order as later anatomy
        # consumers of ``page_features.has_asset``.
        self._ensure_asset_probe()
        if self.ctx.settings.get("stop_after_asset_probe"):
            # Debug Stage-0: bootstrap → coarse VLM → text scan → asset probe.
            return profile
        self._ensure_toc_profile()
        return profile

    def _persist_ready_anatomy(self) -> None:
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
            summary=self.trace.summary(),
        )
        self.trace.flush(
            final_status="ready",
            summary=self.trace.summary(),
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
        with stage_timer(
            "document_profile.asset_probe",
            job_id=self.ctx.job_id,
            page_count=self.blackboard.page_count,
        ):
            for tool_name, handler in (
                ("probe.page_assets", probe_page_assets),
                ("aggregate.doc_stats", aggregate_doc_stats),
            ):
                result: ToolResult = handler(self.ctx, {})
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

    def _run_text_scan(self) -> None:
        profile = self.blackboard.document_profile
        if profile is None:
            raise RuntimeError("document_profile missing; run coarse profile first")
        page_count = int(self.blackboard.page_count or 0)
        pages = list(range(1, page_count + 1))
        if not pages:
            self.blackboard.page_full_text_cache = {}
            self.blackboard.page_text_search_view = None
            return
        bands = read_page_text_bands(
            self.ctx.pdf_path,
            pages,
            header_y=profile.header_y,
            footer_y=profile.footer_y,
            timeout=300,
        )
        self.blackboard.page_full_text_cache = bands
        self.blackboard.page_text_search_view = None
        self.trace.record_step(
            round_index=self.round_index,
            actor="scan:read_page_text_bands",
            action_type="scan",
            result=ToolResult(
                status="ok",
                payload={"page_count": len(bands)},
                output_summary={"page_count": len(bands)},
            ),
            tool_name="read_page_text_bands",
            tool_args={
                "pages": pages,
                "header_y": profile.header_y,
                "footer_y": profile.footer_y,
            },
        )
        self.round_index += 1

    def _ensure_toc_profile(self) -> None:
        if self.blackboard.toc_result is not None:
            return

        try:
            if self._is_slide_deck():
                self._run_slide_structure_route()
            else:
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
        if result.status != "ok":
            raise RuntimeError(result.error or f"{tool_name} failed")
        self.round_index += 1
        return result

    def _is_slide_deck(self) -> bool:
        """Read the coarse page format the way the TOC tools read the profile."""
        routing = getattr(self.blackboard.document_profile, "routing_category", None)
        return PdfRoutingCategory.normalize(routing) is PdfRoutingCategory.SLIDES

    def _run_slide_structure_route(self) -> None:
        result = run_slide_structure_route(self.ctx)
        self.trace.record_step(
            round_index=self.round_index,
            actor="structure:slide_structure",
            action_type="toc",
            result=result,
            tool_name=None,
            tool_args={},
        )
        self.round_index += 1

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

