#!/usr/bin/env python3
# ruff: noqa: E402, F401
"""Shared utilities for the staged page-memory debug scripts.

All stage scripts (debug_pm_stage0..6) import from here instead of
duplicating bootstrap, artifact I/O, and argparse helpers.
"""

import gevent.monkey

gevent.monkey.patch_all()

import argparse
import json
import os
import sys
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── Bootstrap ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[4]
WORKER_ROOT = ROOT / "apps" / "worker"
sys.path.insert(0, str(WORKER_ROOT))
sys.path.insert(0, str(ROOT / "packages" / "shared-python"))

from dotenv import load_dotenv

load_dotenv(WORKER_ROOT / ".env")
os.environ.setdefault("LOCAL_DEBUG", "1")
os.environ.setdefault("OVERSIZED_PDF_SHARD_ENABLED", "true")

from loguru import logger

from app.services.page_memory._utils import (
    page_scope_info,
    scope_id_for_pages,
    sort_skeletons,
)
from app.services.page_memory._serialization import (
    build_hierarchy_tree as _build_hierarchy_from_skeletons,
    derive_hierarchy_page_scope as _derive_hierarchy_page_scope,
    scope_manifest as _scope_manifest,
    serialize_assets as _serialize_assets,
    serialize_hierarchy_artifact as _serialize_hierarchy_artifact,
    serialize_page_tags as _serialize_page_tags,
    serialize_scope_skeletons as _serialize_scope_skeletons,
)
from shared.services.ai.token_tracking import (
    init_token_tracker,
    get_current_token_tracker,
)
from shared.services.ai.token_costing import build_token_cost_estimate

DEFAULT_PDF = Path(
    "/Users/wuchengke/Desktop/temp/test_docs/"
    "SJSYJ-SC-2024 企业制度汇编（上册）.pdf"
)
OUTPUT_ROOT = Path("~/.knowhere/_debug_parse").expanduser()


# ── Token cost tracker ────────────────────────────────────────────────────────


def _usage_delta(prev: dict[str, Any], cur: dict[str, Any]) -> dict[str, Any]:
    _NUM = ("prompt_tokens", "completion_tokens", "total_tokens", "calls")

    def _sub(a: dict, b: dict) -> dict:
        return {f: int(b.get(f, 0)) - int(a.get(f, 0)) for f in _NUM}

    def _bucket(pa: dict, pb: dict) -> dict:
        r: dict[str, Any] = {}
        for k in set(pa) | set(pb):
            pk, ck = pa.get(k, {}), pb.get(k, {})
            if not isinstance(pk, dict) or not isinstance(ck, dict):
                continue
            e = {f: v for f, v in _sub(pk, ck).items() if v}
            pm, cm = pk.get("models", {}), ck.get("models", {})
            if pm or cm:
                md = _bucket(pm, cm)
                if md:
                    e["models"] = md
            if e:
                r[k] = e
        return r

    d = _sub(prev, cur)
    for bk in ("by_model", "by_task"):
        bd = _bucket(prev.get(bk, {}), cur.get(bk, {}))
        if bd:
            d[bk] = bd
    return d


class TokenCostTracker:
    """Incremental token usage & cost tracker for debug pipeline stages."""

    def __init__(self) -> None:
        self._dict = init_token_tracker()
        self._root_gid = self._gid()
        self._prev: dict[str, Any] = deepcopy(self._dict)
        self._stages: list[dict[str, Any]] = []

    @staticmethod
    def _gid() -> int:
        from shared.services.ai.token_tracking import _current_greenlet_id

        return _current_greenlet_id()

    def register_child_thread(self) -> None:
        from shared.services.ai.token_tracking import _root_ids, _lock

        gid = self._gid()
        if gid != self._root_gid:
            with _lock:
                _root_ids[gid] = self._root_gid

    def snapshot_stage(self, stage: str) -> None:
        cur = deepcopy(get_current_token_tracker() or {})
        delta = _usage_delta(self._prev, cur)
        self._stages.append({
            "stage": stage,
            "prompt_tokens": delta.get("prompt_tokens", 0),
            "completion_tokens": delta.get("completion_tokens", 0),
            "total_tokens": delta.get("total_tokens", 0),
            "calls": delta.get("calls", 0),
            "cost": build_token_cost_estimate(delta),
        })
        self._prev = cur

    def total_cost(self) -> dict[str, Any]:
        return build_token_cost_estimate(get_current_token_tracker() or {})

    def stage_summary(self) -> list[dict[str, Any]]:
        return list(self._stages)


@dataclass
class TraceStageAdapter:
    stages: list[dict[str, Any]]

    def record_stage(
        self,
        stage: str,
        *,
        page_info: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
    ) -> None:
        record_stage(
            self.stages,
            stage,
            page_info=page_info,
            variables=variables,
        )


@dataclass
class ScopeResult:
    """Result returned by run_scope_pipeline() for one coarse scope."""

    scope_id: str
    skeletons: list[Any]
    tags: list[Any]
    assets_by_page: dict[int, list[Any]]
    rendered: list[Any]
    final_pages: list[int]
    scope_manifest: dict[str, Any]
    trace_stages: list[dict[str, Any]] = field(default_factory=list)


# ── Argparse helpers ──────────────────────────────────────────────────────────


def base_argparser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--file", default=str(DEFAULT_PDF), help="PDF path")
    parser.add_argument("--model", default=None, help="Override hierarchy/profiler model")
    parser.add_argument("--vlm-model", default=None, help="VLM model override")
    parser.add_argument("--no-vlm", action="store_true", help="Disable VLM calls")
    parser.add_argument(
        "--out-suffix",
        default="",
        help=(
            "Append to the debug doc directory name so runs do not overwrite "
            "the default page_memory tree "
            "(e.g. --out-suffix boundary_clip → …/doc__boundary_clip/page_memory)."
        ),
    )
    return parser


def resolve_paths(args: argparse.Namespace) -> tuple[str, str, Path]:
    """Returns (pdf_path, filename, out_dir)."""
    from app.services.document_parser.orchestration.path_segment import build_parser_path_segment

    pdf_path = str(Path(args.file).expanduser().resolve())
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(pdf_path)
    filename = os.path.basename(pdf_path)
    dir_name = build_parser_path_segment(filename)
    suffix = str(getattr(args, "out_suffix", "") or "").strip()
    if suffix:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in suffix)
        dir_name = f"{dir_name}__{safe}"
    out_dir = OUTPUT_ROOT / dir_name / "page_memory"
    out_dir.mkdir(parents=True, exist_ok=True)
    return pdf_path, filename, out_dir


# ── ToolContext builder ───────────────────────────────────────────────────────


def build_ctx(
    *, pdf_path: str, job_id: str, out_dir: Path,
    page_count: int, page_texts: dict[int, str], vlm_model: str | None,
    asset_extraction_enabled: bool = False,
):
    from app.services.document_agent.manifest import ToolContext
    from app.services.document_agent.state import AgentBlackboard
    from app.services.document_agent.budget import BudgetTracker

    blackboard = AgentBlackboard()
    blackboard.page_count = page_count
    blackboard.page_full_text_cache = dict(page_texts)

    vmodel = vlm_model or os.environ.get("IMAGE_MODEL")
    reason_model = os.environ.get("PAGE_LOCATE_REASON_MODEL") or os.environ.get("NORMOL_MODEL")

    budget = BudgetTracker(plan_budget=50000, visual_budget=200000)
    return ToolContext(
        pdf_path=pdf_path,
        job_id=job_id,
        blackboard=blackboard,
        budget=budget,
        trace=None,
        output_dir=str(out_dir / "_doc_agent"),
        settings={
            "vlm_model": vmodel,
            "model": reason_model,
            "agent_png_dpi": os.environ.get("AGENT_PNG_DPI", "144"),
        },
    )


# ── Anatomy / doc-profile cache ──────────────────────────────────────────────


def resolve_anatomy_cache_path(out_dir: Path) -> Path:
    """Prefer package-root ``doc_profile.json``; fall back to legacy paths."""
    from app.services.document_agent.persist import DOC_PROFILE_FILENAME

    candidates = (
        out_dir / DOC_PROFILE_FILENAME,
        out_dir / "_doc_agent" / DOC_PROFILE_FILENAME,
        out_dir / "_doc_agent" / "anatomy_map.json",
    )
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def load_anatomy_cache(cache_path: Path, pdf_path: str, job_id: str):
    from app.services.document_agent.manifest import (
        H1BoundaryResult,
        H1Candidate,
        PageAnatomyMap,
        PageFeature,
        PageLabel,
        Shard,
        ShardPlan,
        TocAnchorPage,
        TocEvidence,
        TocResult,
        ValidationReport,
    )

    logger.info(f"⏩ Reusing cached anatomy: {cache_path}")
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    toc = data.get("toc_result") or {}
    h1 = data.get("h1_result") or {}
    sp = data.get("shard_plan") or {}

    page_features = []
    for pf in data.get("page_features", []):
        page_features.append(PageFeature(
            page=int(pf.get("page", 0)),
            raw_text_length=int(pf.get("raw_text_length", 0)),
            text_density=float(pf.get("text_density", 0)),
            image_coverage=float(pf.get("image_coverage", 0)),
            image_count=int(pf.get("image_count", 0)),
            table_count=int(pf.get("table_count", 0)),
            drawings_count=int(pf.get("drawings_count", 0)),
            orientation=pf.get("orientation", "portrait"),
            width=float(pf.get("width", 0)),
            height=float(pf.get("height", 0)),
            has_asset=bool(pf.get("has_asset", False)),
            is_blank_like=bool(pf.get("is_blank_like", False)),
        ))
    page_labels = []
    for pl in data.get("page_labels", []):
        page_labels.append(PageLabel(
            page=int(pl.get("page", 0)),
            kind=pl.get("kind", "normal"),
            confidence=float(pl.get("confidence", 0)),
            evidence=pl.get("evidence", {}),
        ))

    candidates = []
    for candidate in toc.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        candidates.append(
            TocAnchorPage(
                page=int(candidate.get("page", 0)),
                png_path=str(candidate.get("png_path") or ""),
                source=candidate.get("source", "text_scan"),
            )
        )
    evidence = []
    for item in toc.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        evidence.append(
            TocEvidence(
                page_index=int(item.get("page_index", 0)),
                source=str(item.get("source") or ""),
                confidence=float(item.get("confidence", 0) or 0),
                reason=str(item.get("reason") or ""),
            )
        )

    return PageAnatomyMap(
        job_id=data.get("job_id", job_id),
        file_path=data.get("file_path", pdf_path),
        page_count=int(data.get("page_count", 0)),
        page_features=page_features,
        page_labels=page_labels,
        toc_result=TocResult(
            toc_pages=list(toc.get("toc_pages", [])),
            candidates=candidates,
            evidence=evidence,
            method=toc.get("method", "none"),
            notes=str(toc.get("notes") or ""),
            failure_kind=toc.get("failure_kind", "none"),
        ),
        h1_result=H1BoundaryResult(
            h1_candidates=[
                H1Candidate(
                    title=c.get("title", ""),
                    page=int(c.get("page", 0)),
                    confidence=float(c.get("confidence", 0) or 0),
                    matched_line=c.get("matched_line", ""),
                    source=c.get("source", "none"),
                )
                for c in h1.get("h1_candidates", [])
            ],
        ),
        shard_plan=ShardPlan(
            enabled=bool(sp.get("enabled", False)),
            reason=sp.get("reason", "not_needed"),
            shards=[
                Shard(
                    shard_index=int(s.get("shard_index", i)),
                    page_start=int(s.get("page_start", 1)),
                    page_end=int(s.get("page_end", 1)),
                    page_offset=int(s.get("page_offset", 0)),
                    anchor_type=s.get("anchor_type", "forced_max_size"),
                    anchor_evidence=s.get("anchor_evidence", ""),
                    confidence=float(s.get("confidence", 0) or 0),
                    toc_hierarchies=(
                        list(s["toc_hierarchies"])
                        if isinstance(s.get("toc_hierarchies"), list)
                        else None
                    ),
                )
                for i, s in enumerate(sp.get("shards", []))
            ],
            validation=ValidationReport(valid=True),
        ),
        toc_hierarchies=data.get("toc_hierarchies"),
        document_profile=_document_profile_from_dict(data.get("document_profile")),
        skeleton_anchor=data.get("skeleton_anchor")
        if isinstance(data.get("skeleton_anchor"), dict)
        else None,
        skeleton_nodes=list(data.get("skeleton_nodes") or [])
        if isinstance(data.get("skeleton_nodes"), list)
        else None,
        pending_skeleton_anchors=list(data.get("pending_skeleton_anchors") or [])
        if isinstance(data.get("pending_skeleton_anchors"), list)
        else [],
        global_signals=dict(data.get("global_signals") or {})
        if isinstance(data.get("global_signals"), dict)
        else {},
    )


# ── Profile ───────────────────────────────────────────────────────────────────


def run_profile(
    pdf_path: str,
    job_id: str,
    out_dir: Path,
    model: str | None,
    *,
    skip_toc_anchoring: bool = False,
):
    """Run page-memory profile exactly like production ``memory_service.run``.

    Uses ``profile_document(..., skip_shard_plan=True, oversized_policy="page_memory")``
    so coarse → anatomy matches the live track (no LLM shard planning).

    ``skip_toc_anchoring=True`` stops after TOC extract (legacy monolithic
    helper). Prefer staged debug: Stage-0 bootstrap then Stage-1 TOC.
    """
    from app.services.document_parser.profiling.doc_profiler import profile_document
    from shared.core.config import settings

    logger.info("=" * 70)
    logger.info(f"🧬 DOC_PROFILE (page_memory, monolithic) — {job_id}")
    logger.info("=" * 70)
    if skip_toc_anchoring:
        logger.info("   skip_toc_anchoring=True (TOC extract only; no calibration)")

    previous_image_model = settings.IMAGE_MODEL
    if model:
        settings.IMAGE_MODEL = model
        logger.info(f"   IMAGE_MODEL override → {model}")

    t0 = time.time()
    try:
        profile = profile_document(
            pdf_path,
            job_id,
            job_id=job_id,
            output_dir=str(out_dir),
            skip_shard_plan=True,
            oversized_policy="page_memory",
            skip_toc_anchoring=skip_toc_anchoring,
        )
    finally:
        if model:
            settings.IMAGE_MODEL = previous_image_model

    logger.info(f"   profile done in {time.time() - t0:.1f}s")
    logger.info(
        "   category={} routing={} page_count={} is_atlas={}",
        profile.category,
        getattr(profile.routing_category, "value", profile.routing_category),
        profile.page_count,
        profile.is_atlas,
    )

    anatomy = profile.anatomy
    if anatomy is None:
        raise RuntimeError(
            "page_memory profile returned no anatomy "
            f"(routing={profile.routing_category}). Atlas / no-anatomy path "
            "cannot continue Stage 1."
        )

    from app.services.document_agent.persist import DOC_PROFILE_FILENAME

    profile_path = out_dir / DOC_PROFILE_FILENAME
    if not profile_path.exists():
        write_debug_json(profile_path, anatomy.to_dict())

    asset_pages = sum(1 for f in anatomy.page_features if getattr(f, "has_asset", False))
    logger.info(f"   page_count={anatomy.page_count}")
    logger.info(f"   toc_pages={anatomy.toc_result.toc_pages}")
    logger.info(f"   has_asset_pages={asset_pages}/{anatomy.page_count}")
    if anatomy.h1_result:
        logger.info(f"   h1_candidates={len(anatomy.h1_result.h1_candidates)}")
    logger.info(
        "   shard_plan.enabled={} shards={}",
        anatomy.shard_plan.enabled,
        len(anatomy.shard_plan.shards),
    )
    logger.info(f"   doc_profile → {profile_path}")
    return anatomy


def _build_debug_coordinator(
    *,
    pdf_path: str,
    job_id: str,
    out_dir: Path,
    model: str | None,
    settings_extra: dict[str, Any] | None = None,
):
    """Build a ProfileCoordinator with the same models as production page_memory."""
    from app.services.document_agent.coordinator import ProfileCoordinator
    from shared.core.config import settings

    if model:
        settings.IMAGE_MODEL = model
    agent_output_dir = out_dir / "_doc_agent"
    agent_output_dir.mkdir(parents=True, exist_ok=True)
    merged = {
        "vlm_model": settings.IMAGE_MODEL,
        "toc_profile_enabled": True,
        "model": settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL,
    }
    if settings_extra:
        merged.update(settings_extra)
    return ProfileCoordinator(
        pdf_path=pdf_path,
        job_id=job_id,
        output_dir=str(agent_output_dir),
        db=None,
        model=settings.IMAGE_MODEL,
        settings=merged,
    )


def _document_profile_from_dict(data: dict[str, Any] | None):
    from app.services.document_agent.manifest import DocumentProfile

    raw = data or {}
    return DocumentProfile(
        is_scanned=bool(raw.get("is_scanned", False)),
        category=str(raw.get("category") or "unknown"),
        routing_category=str(raw.get("routing_category") or "generic"),
        language=str(raw.get("language") or "unknown"),
        rationale=str(raw.get("rationale") or ""),
        header_y=raw.get("header_y"),
        footer_y=raw.get("footer_y"),
    )


def _page_features_from_dicts(rows: list[Any]) -> list[Any]:
    from app.services.document_agent.manifest import PageFeature

    out = []
    for pf in rows:
        if not isinstance(pf, dict):
            continue
        out.append(
            PageFeature(
                page=int(pf.get("page", 0)),
                raw_text_length=int(pf.get("raw_text_length", 0)),
                text_density=float(pf.get("text_density", 0)),
                image_coverage=float(pf.get("image_coverage", 0)),
                image_count=int(pf.get("image_count", 0)),
                table_count=int(pf.get("table_count", 0)),
                drawings_count=int(pf.get("drawings_count", 0)),
                orientation=pf.get("orientation", "portrait"),
                width=float(pf.get("width", 0)),
                height=float(pf.get("height", 0)),
                has_asset=bool(pf.get("has_asset", False)),
                is_blank_like=bool(pf.get("is_blank_like", False)),
                invisible_text_length=int(pf.get("invisible_text_length", 0) or 0),
            )
        )
    return out


def _page_labels_from_dicts(rows: list[Any]) -> list[Any]:
    from app.services.document_agent.manifest import PageLabel

    out = []
    for pl in rows:
        if not isinstance(pl, dict):
            continue
        out.append(
            PageLabel(
                page=int(pl.get("page", 0)),
                kind=pl.get("kind", "normal"),
                confidence=float(pl.get("confidence", 0)),
                evidence=dict(pl.get("evidence") or {}),
            )
        )
    return out


def persist_stage0_state(out_dir: Path, coordinator) -> Path:
    """Persist Stage-0 blackboard so Stage-1 can resume at Find."""
    doc_agent_dir = out_dir / "_doc_agent"
    doc_agent_dir.mkdir(parents=True, exist_ok=True)

    texts = {
        str(page): text
        for page, text in dict(coordinator.blackboard.page_full_text_cache or {}).items()
    }
    write_debug_json(page_text_cache_path(out_dir), texts)

    profile = coordinator.blackboard.document_profile
    state = {
        "version": "1.0",
        "page_count": int(coordinator.blackboard.page_count or 0),
        "document_profile": profile.to_dict() if profile is not None else None,
        "page_features": [
            feature.to_dict() for feature in (coordinator.blackboard.page_features or [])
        ],
        "page_labels": [
            label.to_dict() for label in (coordinator.blackboard.page_labels or [])
        ],
        "doc_stats": dict(coordinator.blackboard.doc_stats or {}),
        "global_signals": dict(coordinator.blackboard.global_signals or {}),
        "page_full_text_cache_path": PAGE_TEXT_CACHE_NAME,
    }
    path = stage0_state_path(out_dir)
    write_debug_json(path, state)
    return path


def load_stage0_into_coordinator(coordinator, out_dir: Path) -> None:
    """Restore Stage-0 outputs onto a coordinator blackboard."""
    state_path = stage0_state_path(out_dir)
    require_file(
        state_path,
        hint=(
            "Run Stage 0 first: uv run python "
            "scripts/page_memory/debug_pm_stage0_bootstrap.py --file ..."
        ),
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    text_path = page_text_cache_path(out_dir)
    require_file(
        text_path,
        hint="Stage-0 page_full_text_cache.json missing; re-run Stage 0",
    )
    raw_texts = json.loads(text_path.read_text(encoding="utf-8"))
    page_texts = {int(page): str(text) for page, text in dict(raw_texts or {}).items()}

    bb = coordinator.blackboard
    bb.page_count = int(state.get("page_count") or 0)
    bb.document_profile = _document_profile_from_dict(state.get("document_profile"))
    bb.page_features = _page_features_from_dicts(list(state.get("page_features") or []))
    bb.page_labels = _page_labels_from_dicts(list(state.get("page_labels") or []))
    bb.doc_stats = dict(state.get("doc_stats") or {})
    bb.global_signals = dict(state.get("global_signals") or {})
    bb.page_full_text_cache = page_texts
    # Stage-1 owns TOC + assets from here.
    bb.toc_result = None
    bb.toc_hierarchies = None
    bb.skeleton_anchor = None
    bb.skeleton_nodes = None
    bb.pending_skeleton_anchors = []
    bb.global_signals.pop("toc_profile_attempted", None)
    # Keep assets_probed / has_asset from Stage-0; Stage-1 is TOC-only.
    logger.info(
        "⏩ Resumed Stage-0: pages={} text_pages={} labels={} assets_probed={}",
        bb.page_count,
        len(page_texts),
        len(bb.page_labels),
        bool(bb.global_signals.get("assets_probed")),
    )


def run_stage0_bootstrap(
    pdf_path: str,
    job_id: str,
    out_dir: Path,
    model: str | None,
):
    """Production-aligned Stage-0: bootstrap → coarse VLM → text scan → asset probe."""
    from shared.core.config import settings

    logger.info("=" * 70)
    logger.info(
        f"🧬 Stage 0: BOOTSTRAP + COARSE VLM + TEXT SCAN + ASSET PROBE — {job_id}"
    )
    logger.info("=" * 70)

    previous_image_model = settings.IMAGE_MODEL
    t0 = time.time()
    try:
        coordinator = _build_debug_coordinator(
            pdf_path=pdf_path,
            job_id=job_id,
            out_dir=out_dir,
            model=model,
            settings_extra={"stop_after_asset_probe": True},
        )
        profile = coordinator.run_coarse()
        state_path = persist_stage0_state(out_dir, coordinator)
        asset_pages = sum(
            1
            for feature in (coordinator.blackboard.page_features or [])
            if getattr(feature, "has_asset", False)
        )
        update_pipeline_state(
            pipeline_state_path(out_dir),
            stage=0,
            document={
                "source_file_name": job_id,
                "page_count": coordinator.blackboard.page_count,
                "stage0_state": str(state_path),
            },
            payload={
                "page_count": coordinator.blackboard.page_count,
                "is_scanned": bool(getattr(profile, "is_scanned", False)),
                "routing_category": getattr(profile, "routing_category", None),
                "text_pages": len(coordinator.blackboard.page_full_text_cache or {}),
                "assets_probed": bool(
                    coordinator.blackboard.global_signals.get("assets_probed")
                ),
                "has_asset_pages": asset_pages,
            },
        )
    finally:
        if model:
            settings.IMAGE_MODEL = previous_image_model

    logger.info(f"   stage0 done in {time.time() - t0:.1f}s → {state_path}")
    logger.info(
        "   category={} routing={} page_count={} is_scanned={} has_asset_pages={}",
        getattr(profile, "category", None),
        getattr(profile, "routing_category", None),
        coordinator.blackboard.page_count,
        getattr(profile, "is_scanned", None),
        asset_pages,
    )
    return coordinator, profile, state_path


def run_stage1_toc(
    pdf_path: str,
    job_id: str,
    out_dir: Path,
    model: str | None,
):
    """Production-aligned Stage-1: Find → extract (no calibration).

    Resumes Stage-0 blackboard (including asset probe). Skips
    ``run_toc_anchoring`` (Stage-2). Does not re-run asset probe.
    """
    from app.services.document_agent.persist import (
        DOC_PROFILE_FILENAME,
        build_anatomy_map,
        persist_anatomy_map,
    )
    from app.services.document_agent.validators import single_shard_plan
    from shared.core.config import settings

    logger.info("=" * 70)
    logger.info(f"🧬 Stage 1: TOC FIND → EXTRACT — {job_id}")
    logger.info("=" * 70)

    previous_image_model = settings.IMAGE_MODEL
    t0 = time.time()
    try:
        coordinator = _build_debug_coordinator(
            pdf_path=pdf_path,
            job_id=job_id,
            out_dir=out_dir,
            model=model,
            settings_extra={"skip_toc_anchoring": True},
        )
        load_stage0_into_coordinator(coordinator, out_dir)
        coordinator._ensure_toc_profile(strict=False)
        coordinator.blackboard.shard_plan = single_shard_plan(
            coordinator.blackboard.page_count
        )
        anatomy = build_anatomy_map(coordinator.ctx)
        persist_anatomy_map(coordinator.ctx, {})
        profile_path = out_dir / DOC_PROFILE_FILENAME
        write_debug_json(profile_path, anatomy.to_dict())
        # Canonical profile is at package root; drop nested duplicate.
        try:
            (out_dir / "_doc_agent" / "anatomy_map.json").unlink()
        except FileNotFoundError:
            pass
        update_pipeline_state(
            pipeline_state_path(out_dir),
            stage=1,
            document={
                "source_file_name": job_id,
                "page_count": anatomy.page_count,
                "anatomy_path": str(profile_path),
            },
            payload={
                "toc_pages": list(getattr(anatomy.toc_result, "toc_pages", []) or []),
                "region_count": len(list(anatomy.toc_hierarchies or [])),
                "skip_toc_anchoring": True,
            },
        )
    finally:
        if model:
            settings.IMAGE_MODEL = previous_image_model

    logger.info(f"   stage1 done in {time.time() - t0:.1f}s")
    logger.info(f"   toc_pages={anatomy.toc_result.toc_pages}")
    logger.info(f"   doc_profile → {profile_path}")
    return anatomy


# ── JSON / artifact I/O ───────────────────────────────────────────────────────


def jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if hasattr(value, "value"):
        return getattr(value, "value")
    return value


def write_debug_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(jsonable(value), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"   debug json → {path}")


def toc_hierarchies_to_hierarchy_tree(
    toc_hierarchies: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Build Stage-1 human TOC dump in final ``HIERARCHY`` shape.

    Uses original VLM headings (keeps numbering prefixes). Does **not** run
    ``extract_toc_nodes`` / ``clean_toc_title`` — those are locate-time only.
    Regions are concatenated in profile order so multi-TOC docs still form one
    nested tree readable like ``hierarchy.json``.
    """
    from app.services.document_agent.tools.vlm_toc_extractor import build_toc_tree

    flat_entries: list[dict[str, Any]] = []
    for region in toc_hierarchies or []:
        if not isinstance(region, dict):
            continue
        rows = region.get("toc_with_level") or []
        if rows:
            for entry in rows:
                if not isinstance(entry, dict):
                    continue
                heading = str(entry.get("heading") or entry.get("title") or "").strip()
                if not heading:
                    continue
                flat_entries.append(
                    {
                        "title": heading,
                        "level": entry.get("level", 1),
                        "page_number": entry.get("page_number"),
                    }
                )
            continue
        # Fallback: region only stored ``toc_tree`` (already nested, original keys).
        tree = region.get("toc_tree")
        if isinstance(tree, dict) and tree:
            flat_entries.extend(_flatten_hierarchy_tree_entries(tree))
    return build_toc_tree(flat_entries)


def _flatten_hierarchy_tree_entries(
    tree: dict[str, Any],
    *,
    level: int = 1,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for title, children in tree.items():
        heading = str(title or "").strip()
        if not heading:
            continue
        entries.append({"title": heading, "level": level})
        if isinstance(children, dict) and children:
            entries.extend(
                _flatten_hierarchy_tree_entries(children, level=level + 1)
            )
    return entries


def write_toc_hierarchy_artifact(
    out_dir: Path,
    *,
    hierarchy_tree: dict[str, Any],
    stats: dict[str, Any] | None = None,
) -> Path:
    """Write Stage-1 TOC as human-readable ``HIERARCHY`` at package root.

    Debug-only for page_memory (not packaged into ZIP). Shape matches final
    ``hierarchy.json`` / ``manifest.HIERARCHY``; titles keep TOC prefixes.
    """
    path = out_dir / "toc_hierarchy.json"
    # Legacy list dump from an earlier debug format.
    for legacy_name in ("toc_hierarchies.json",):
        legacy = out_dir / legacy_name
        try:
            legacy.unlink()
        except FileNotFoundError:
            pass
    write_debug_json(
        path,
        {
            "HIERARCHY": hierarchy_tree or {},
            "stats": dict(stats or {}),
        },
    )
    return path


PIPELINE_STATE_VERSION = "1.0"
PIPELINE_STATE_NAME = "pipeline_state.json"
_PIPELINE_STAGES = tuple(f"stage{number}" for number in range(0, 7))


def pipeline_state_path(out_dir: Path) -> Path:
    return out_dir / "_doc_agent" / PIPELINE_STATE_NAME


STAGE0_STATE_NAME = "stage0_state.json"
PAGE_TEXT_CACHE_NAME = "page_full_text_cache.json"


def stage0_state_path(out_dir: Path) -> Path:
    return out_dir / "_doc_agent" / STAGE0_STATE_NAME


def page_text_cache_path(out_dir: Path) -> Path:
    return out_dir / "_doc_agent" / PAGE_TEXT_CACHE_NAME


def load_pipeline_state(
    state_path: Path,
    *,
    legacy_locate_cache: Path | None = None,
) -> dict[str, Any]:
    """Load the shared Stage 0-6 ledger, with locate-cache compatibility."""
    if state_path.exists():
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"pipeline state must be an object: {state_path}")
        data.setdefault("version", PIPELINE_STATE_VERSION)
        data.setdefault("stages", {})
        return data

    if legacy_locate_cache is not None and legacy_locate_cache.exists():
        rows = json.loads(legacy_locate_cache.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError(
                f"legacy locate cache must be a list: {legacy_locate_cache}"
            )
        logger.warning(
            "Legacy locate cache detected; it will be migrated on the next stage write: {}",
            legacy_locate_cache,
        )
        return {
            "version": PIPELINE_STATE_VERSION,
            "stages": {
                "stage2": {
                    "status": "legacy",
                    "skeletons": rows,
                }
            },
        }

    raise FileNotFoundError(state_path)


def _pipeline_skeleton_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    stages = state.get("stages")
    stage2 = stages.get("stage2") if isinstance(stages, dict) else None
    rows = stage2.get("skeletons") if isinstance(stage2, dict) else None
    if rows is None:
        # Compatibility with the short-lived ``stage2_state.json`` proposal.
        rows = state.get("skeletons")
    if not isinstance(rows, list):
        raise ValueError("pipeline state is missing stages.stage2.skeletons[]")
    return [row for row in rows if isinstance(row, dict)]


def load_pipeline_skeletons(
    state_path: Path,
    *,
    legacy_locate_cache: Path | None = None,
) -> list[Any]:
    from app.services.page_memory.skeleton_extractor import SectionSkeleton

    state = load_pipeline_state(
        state_path,
        legacy_locate_cache=legacy_locate_cache,
    )
    return [
        SectionSkeleton(
            section_path=str(row["section_path"]),
            title=str(row["title"]),
            level=int(row["level"]),
            start_page=int(row["start_page"]),
            end_page=int(row["end_page"]),
            parent_path=row.get("parent_path"),
            evidence=dict(row.get("evidence") or {}),
        )
        for row in _pipeline_skeleton_rows(state)
    ]


def update_pipeline_state(
    state_path: Path,
    *,
    stage: int,
    payload: dict[str, Any],
    document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically update one stage and invalidate stale later-stage entries."""
    stage_key = f"stage{stage}"
    if stage_key not in _PIPELINE_STAGES:
        raise ValueError(f"unsupported pipeline stage: {stage}")

    if state_path.exists():
        state = load_pipeline_state(state_path)
    else:
        state = {
            "version": PIPELINE_STATE_VERSION,
            "stages": {},
        }
    stages = state.setdefault("stages", {})
    if not isinstance(stages, dict):
        raise ValueError(f"pipeline state stages must be an object: {state_path}")

    stage_index = _PIPELINE_STAGES.index(stage_key)
    for stale_key in _PIPELINE_STAGES[stage_index + 1 :]:
        stages.pop(stale_key, None)

    updated_at = datetime.now(timezone.utc).isoformat()
    stages[stage_key] = {
        "status": "complete",
        "updated_at": updated_at,
        **jsonable(payload),
    }
    if document:
        state["document"] = {
            **dict(state.get("document") or {}),
            **jsonable(document),
        }
    state["updated_at"] = updated_at

    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(f"{state_path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(state_path)
    logger.info("   pipeline state → {}", state_path)
    return state


def remove_legacy_doc_agent_artifacts(
    doc_agent_dir: Path,
    *,
    include_stage2: bool = False,
    keep_resume_cache: bool = True,
) -> None:
    """Drop nested doc-agent clutter; keep resume + pipeline history by default.

    Canonical package artifacts live at ``page_memory/`` root
    (``doc_profile.json``, ``trace.json``). Nested ``anatomy_map.json`` and
    calibration page PNGs are duplicates / inspect leftovers.
    """
    import shutil

    names = {
        "parser_profile.json",
        "toc_hierarchies.json",
        "anatomy_map.json",
        "trace.json",
        "doc_profile.json",
    }
    if include_stage2:
        names.update(
            {
                "calibration_result.json",
                "null_page_parent_locate.json",
                "locate_cache.json",
                "stage2_state.json",
            }
        )
    if not keep_resume_cache:
        names.update(
            {
                STAGE0_STATE_NAME,
                PAGE_TEXT_CACHE_NAME,
                "stage_costs.json",
            }
        )
    for name in names:
        try:
            (doc_agent_dir / name).unlink()
        except FileNotFoundError:
            pass
    for dirname in ("coarse_assets", "calibration_inspect"):
        legacy_dir = doc_agent_dir / dirname
        if legacy_dir.is_dir():
            shutil.rmtree(legacy_dir)
    try:
        (doc_agent_dir / "coarse_assets.html").unlink()
    except FileNotFoundError:
        pass


def record_stage(
    stages: list[dict[str, Any]],
    stage: str,
    *,
    page_info: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
) -> None:
    stages.append(
        {
            "stage": stage,
            "page_info": page_info or {},
            "variables": variables or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )


STAGE_COSTS_VERSION = "1.0"
STAGE_COSTS_NAME = "stage_costs.json"
_COST_STAGE_KEYS = tuple(f"stage{number}" for number in range(0, 7))


def stage_costs_path(out_dir: Path) -> Path:
    return out_dir / "_doc_agent" / STAGE_COSTS_NAME


def load_stage_costs(out_dir: Path) -> dict[str, Any]:
    path = stage_costs_path(out_dir)
    if not path.exists():
        return {"version": STAGE_COSTS_VERSION, "stages": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"stage costs must be an object: {path}")
    data.setdefault("version", STAGE_COSTS_VERSION)
    data.setdefault("stages", {})
    return data


def _stage_usage_snapshot(tracker: TokenCostTracker | None) -> dict[str, Any]:
    usage = get_current_token_tracker() or {}
    return {
        "token_usage": deepcopy(usage),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "calls": int(usage.get("calls") or 0),
        "cost": (
            tracker.total_cost()
            if tracker is not None
            else build_token_cost_estimate(usage)
        ),
        "by_substage": tracker.stage_summary() if tracker is not None else [],
    }


def _merge_token_usage(
    destination: dict[str, Any],
    source: dict[str, Any],
) -> None:
    """Merge raw production token-tracker snapshots recursively."""
    for key, value in source.items():
        if isinstance(value, dict):
            child = destination.setdefault(str(key), {})
            if isinstance(child, dict):
                _merge_token_usage(child, value)
        elif isinstance(value, int | float) and not isinstance(value, bool):
            destination[str(key)] = destination.get(str(key), 0) + value


def record_stage_cost(
    out_dir: Path,
    *,
    pipeline_stage: int,
    elapsed_s: float,
    token_cost_tracker: TokenCostTracker | None = None,
    trace_stages: list[dict[str, Any]] | None = None,
    stop_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one pipeline stage's elapsed/cost ledger entry independently."""
    stage_key = f"stage{pipeline_stage}"
    if stage_key not in _COST_STAGE_KEYS:
        raise ValueError(f"unsupported cost pipeline stage: {pipeline_stage}")

    ledger = load_stage_costs(out_dir)
    stages = ledger.setdefault("stages", {})
    if not isinstance(stages, dict):
        raise ValueError(f"stage costs stages must be an object: {stage_costs_path(out_dir)}")

    stage_index = _COST_STAGE_KEYS.index(stage_key)
    for stale_key in _COST_STAGE_KEYS[stage_index + 1 :]:
        stages.pop(stale_key, None)

    usage = _stage_usage_snapshot(token_cost_tracker)
    updated_at = datetime.now(timezone.utc).isoformat()
    entry: dict[str, Any] = {
        "status": "complete",
        "elapsed_s": round(float(elapsed_s), 3),
        "stop_at": stop_at,
        "updated_at": updated_at,
        **usage,
    }
    if trace_stages is not None:
        entry["trace_stages"] = list(trace_stages)
    if extra:
        entry.update(jsonable(extra))
    stages[stage_key] = entry
    ledger["updated_at"] = updated_at

    path = stage_costs_path(out_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)
    logger.info(
        "   stage cost → {} ({} {:.1f}s / ${:.6f})",
        path,
        stage_key,
        entry["elapsed_s"],
        float((entry.get("cost") or {}).get("total_cost") or 0),
    )
    return ledger


def aggregate_stage_costs(ledger: dict[str, Any]) -> dict[str, Any]:
    """Roll up independently stored stage cost entries for TRACE.JSON."""
    stages = ledger.get("stages") if isinstance(ledger, dict) else {}
    if not isinstance(stages, dict):
        stages = {}

    by_pipeline_stage: dict[str, Any] = {}
    by_substage: list[dict[str, Any]] = []
    merged_trace_stages: list[dict[str, Any]] = []
    usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
        "by_model": {},
        "by_task": {},
    }
    elapsed_s = 0.0

    for stage_key in _COST_STAGE_KEYS:
        row = stages.get(stage_key)
        if not isinstance(row, dict):
            continue
        stage_elapsed = float(row.get("elapsed_s") or 0)
        elapsed_s += stage_elapsed
        for usage_key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "calls",
        ):
            usage[usage_key] += int(row.get(usage_key) or 0)
        raw_usage = row.get("token_usage")
        if isinstance(raw_usage, dict):
            # Numeric top-level fields were already added above.
            _merge_token_usage(
                usage,
                {
                    key: value
                    for key, value in raw_usage.items()
                    if key not in {
                        "prompt_tokens",
                        "completion_tokens",
                        "total_tokens",
                        "calls",
                    }
                },
            )
        stage_cost = row.get("cost") if isinstance(row.get("cost"), dict) else {}
        by_pipeline_stage[stage_key] = {
            "elapsed_s": stage_elapsed,
            "stop_at": row.get("stop_at"),
            "prompt_tokens": int(row.get("prompt_tokens") or 0),
            "completion_tokens": int(row.get("completion_tokens") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
            "calls": int(row.get("calls") or 0),
            "cost": stage_cost,
            "updated_at": row.get("updated_at"),
        }
        for item in row.get("by_substage") or []:
            if isinstance(item, dict):
                by_substage.append(
                    {
                        "pipeline_stage": stage_key,
                        **item,
                    }
                )
        for item in row.get("trace_stages") or []:
            if isinstance(item, dict):
                merged_trace_stages.append(
                    {
                        **item,
                        "pipeline_stage": item.get("pipeline_stage") or stage_key,
                    }
                )

    total_estimate = build_token_cost_estimate(usage)
    return {
        "elapsed_s": round(elapsed_s, 3),
        "completed_pipeline_stages": list(by_pipeline_stage),
        "token_usage": usage,
        "token_cost": {
            "total": {
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "total_tokens": usage["total_tokens"],
                "calls": usage["calls"],
                **total_estimate,
            },
            "by_pipeline_stage": by_pipeline_stage,
            "by_stage": by_substage,
        },
        "stages": merged_trace_stages,
    }


def build_production_job_metadata_from_stage_costs(
    *,
    page_count: int,
    ledger: dict[str, Any],
) -> dict[str, Any]:
    """Project split debug runs onto the production ZIP manifest contract."""
    aggregated = aggregate_stage_costs(ledger)
    stages = ledger.get("stages") if isinstance(ledger, dict) else {}
    if not isinstance(stages, dict):
        stages = {}

    timing_ms: dict[str, int] = {}
    completed_at: datetime | None = None
    for stage_key in _COST_STAGE_KEYS:
        row = stages.get(stage_key)
        if not isinstance(row, dict):
            continue
        timing_ms[stage_key] = int(
            round(float(row.get("elapsed_s") or 0) * 1000)
        )
        raw_updated_at = row.get("updated_at")
        if isinstance(raw_updated_at, str):
            try:
                timestamp = datetime.fromisoformat(raw_updated_at)
            except ValueError:
                continue
            if completed_at is None or timestamp > completed_at:
                completed_at = timestamp

    duration_ms = int(round(float(aggregated.get("elapsed_s") or 0) * 1000))
    completed_at = completed_at or datetime.now(timezone.utc)
    started_at = completed_at - timedelta(milliseconds=duration_ms)
    return {
        "page_count": page_count,
        "parse_track": "page_memory",
        "billing_status": None,
        "billing_amount_micro_dollars": None,
        "billing_credits": None,
        "processing_started_at": started_at.isoformat(),
        "processing_completed_at": completed_at.isoformat(),
        "processing_duration_ms": duration_ms,
        "stages": {
            "timing_ms": timing_ms,
            "token_usage": aggregated.get("token_usage") or {},
        },
    }


def write_trace(
    *,
    out_dir: Path,
    stages: list[dict[str, Any]],
    final_status: str,
    summary: dict[str, Any],
) -> None:
    write_debug_json(
        out_dir / "trace.json",
        {
            "final_status": final_status,
            "summary": summary,
            "stages": stages,
        },
    )


def stop_with_trace(
    *,
    out_dir: Path,
    stages: list[dict[str, Any]],
    stop_at: str,
    page_count: int | None = None,
    scope_id: str | None = None,
    pipeline_stage: int | None = None,
    elapsed_s: float | None = None,
    token_cost_tracker: TokenCostTracker | None = None,
    final_status: str | None = None,
    extra_summary: dict[str, Any] | None = None,
) -> int:
    """Write TRACE.JSON, optionally recording this pipeline stage's cost ledger."""
    if pipeline_stage is not None and elapsed_s is not None:
        record_stage_cost(
            out_dir,
            pipeline_stage=pipeline_stage,
            elapsed_s=elapsed_s,
            token_cost_tracker=token_cost_tracker,
            trace_stages=stages,
            stop_at=stop_at,
        )

    aggregated = aggregate_stage_costs(load_stage_costs(out_dir))
    merged_stages = aggregated.get("stages") or stages
    summary: dict[str, Any] = {
        "page_count": page_count,
        "scope_id": scope_id,
        "rows_count": None,
        "elapsed_s": aggregated.get("elapsed_s"),
        "completed_pipeline_stages": aggregated.get("completed_pipeline_stages"),
        "token_cost": aggregated.get("token_cost"),
    }
    if extra_summary:
        summary.update(jsonable(extra_summary))

    write_trace(
        out_dir=out_dir,
        stages=merged_stages,
        final_status=final_status or f"stopped_at_{stop_at}",
        summary=summary,
    )
    remove_nested_doc_agent_trace(out_dir)
    remove_legacy_doc_agent_artifacts(out_dir / "_doc_agent", include_stage2=True)
    maybe_purge_debug_visuals(out_dir)
    return 0


def remove_nested_doc_agent_trace(out_dir: Path) -> None:
    try:
        (out_dir / "_doc_agent" / "trace.json").unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.debug(f"failed to remove nested doc-agent trace: {exc}")


def maybe_purge_debug_visuals(out_dir: Path) -> None:
    from app.services.document_agent.visual import (
        purge_debug_visual_dirs,
        visual_debug_enabled,
    )

    if not visual_debug_enabled():
        purge_debug_visual_dirs(str(out_dir))


def write_scope_artifacts(
    *,
    out_dir: Path,
    scope_id: str,
    scope_manifest: dict[str, Any],
    hierarchy: list[Any],
    tags: list[Any] | None = None,
    assets_by_page: dict[int, list[Any]] | None = None,
) -> None:
    """Write per-scope viewing artifacts (no standalone scope.json).

    ``tags`` / ``assets_by_page`` of ``None`` leave existing files untouched.
    """
    scope_dir = out_dir / "scopes" / scope_id
    write_debug_json(
        scope_dir / "fine_hierarchy.json",
        _serialize_hierarchy_artifact(hierarchy, scope_manifest_data=scope_manifest),
    )
    if tags is not None:
        write_debug_json(scope_dir / "page_tags.json", _serialize_page_tags(tags))
    if assets_by_page is not None:
        write_debug_json(scope_dir / "assets.json", _serialize_assets(assets_by_page))


def write_top_level_artifacts(
    *,
    out_dir: Path,
    hierarchy: list[Any],
    tags: list[Any],
    assets_by_page: dict[int, list[Any]] | None = None,
) -> None:
    write_debug_json(out_dir / "hierarchy.json", _serialize_hierarchy_artifact(hierarchy))
    write_debug_json(out_dir / "page_tags.json", _serialize_page_tags(tags))
    if assets_by_page is not None:
        write_debug_json(out_dir / "assets.json", _serialize_assets(assets_by_page))
    else:
        try:
            (out_dir / "assets.json").unlink()
        except FileNotFoundError:
            pass


def cleanup_page_memory_artifacts(out_dir: Path) -> None:
    stale_files = {
        "assets.json",
        "chunks.json",
        "coarse_scopes.json",
        "doc_nav.json",
        "hierarchy.json",
        "manifest.json",
        "node_rows.csv",
        "node_rows.json",
        "page_plans.json",
        "page_rendered.json",
        "page_tags.json",
        "report.md",
        "trace.json",
    }
    for name in stale_files:
        path = out_dir / name
        try:
            if path.is_file():
                path.unlink()
        except Exception:
            logger.debug(f"cleanup failed for {path}")
    for name in ("asset_annotate", "debug", "images", "pages", "scopes", "tables"):
        path = out_dir / name
        try:
            if path.is_dir():
                import shutil

                shutil.rmtree(path)
        except Exception:
            logger.debug(f"cleanup failed for {path}")


# ── Tree helpers ──────────────────────────────────────────────────────────────


def walk(nodes: list, depth: int = 0) -> list[tuple[int, Any]]:
    rows: list[tuple[int, Any]] = []
    for node in nodes:
        rows.append((depth, node))
        rows.extend(walk(node.children, depth + 1))
    return rows


def walk_node_count(nodes: list) -> int:
    return len(walk(nodes))


def hierarchy_metrics(nodes: list, *, source: str) -> dict[str, Any]:
    rows = walk(nodes)
    depths = [depth + 1 for depth, _node in rows]
    return {
        "hierarchy_source": source,
        "title_node_count": len(rows),
        "title_leaf_count": sum(1 for _depth, node in rows if not node.children),
        "title_max_depth": max(depths) if depths else 0,
    }


# ── Artifact loaders ──────────────────────────────────────────────────────────


def load_hierarchy_artifact(path: Path) -> tuple[dict[str, Any], list[Any]]:
    """Load a fine hierarchy artifact and its canonical scope manifest."""
    from app.services.page_memory.skeleton_extractor import SectionSkeleton

    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(nodes, list):
        raise ValueError(f"fine hierarchy artifact missing nodes: {path}")
    skeletons: list[SectionSkeleton] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        section_path = str(node.get("section_path") or "").strip()
        title = str(node.get("title") or "").strip()
        if not section_path or not title:
            continue
        skeletons.append(
            SectionSkeleton(
                section_path=section_path,
                title=title,
                level=int(node.get("level") or 1),
                start_page=int(node.get("start_page") or 1),
                end_page=int(node.get("end_page") or node.get("start_page") or 1),
                parent_path=node.get("parent_path"),
                evidence=dict(node.get("evidence") or {}),
            )
        )
    scope = data.get("scope") if isinstance(data, dict) else None
    return dict(scope) if isinstance(scope, dict) else {}, sort_skeletons(skeletons)


def load_skeletons_from_hierarchy_artifact(path: Path) -> list[Any]:
    """Compatibility reader for callers that only need hierarchy nodes."""
    _scope, skeletons = load_hierarchy_artifact(path)
    return skeletons


def load_page_tags_artifact(path: Path) -> list[Any]:
    """Load ``page_tags.json`` written by ``serialize_page_tags``."""
    from app.services.page_memory.page_tagger import PageTagResult

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("tags"), list):
        rows = payload["tags"]
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError(f"page tags artifact has an unsupported schema: {path}")
    tags: list[PageTagResult] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        tags.append(
            PageTagResult(
                page_index=int(item.get("page_index") or 0),
                summary=str(item.get("summary") or ""),
                keywords=list(item.get("keywords") or []),
                strategy_used=str(item.get("strategy_used") or ""),
                entities=list(item.get("entities") or []),
                observed_titles=list(item.get("observed_titles") or []),
            )
        )
    return tags


def load_assets_artifact(path: Path) -> dict[int, list[Any]]:
    from app.services.page_memory.page_assets import PageAsset

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"assets artifact must be a list: {path}")
    assets_by_page: dict[int, list[PageAsset]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        page_index = int(item.get("page_index") or 0)
        raw_source_pages = item.get("source_page_nums")
        source_page_nums = (
            [int(page) for page in raw_source_pages]
            if isinstance(raw_source_pages, list)
            else []
        )
        asset = PageAsset(
            asset_id=str(item.get("asset_id") or ""),
            page_index=page_index,
            asset_index=int(item.get("asset_index") or 0),
            kind=str(item.get("kind") or "figure"),
            bbox_px=[int(v) for v in (item.get("bbox_px") or [])],
            width_px=int(item.get("width_px") or 0),
            height_px=int(item.get("height_px") or 0),
            width_pt=float(item.get("width_pt") or 0),
            height_pt=float(item.get("height_pt") or 0),
            confidence=float(item.get("confidence") or 0),
            title=str(item.get("title") or ""),
            summary=str(item.get("summary") or ""),
            keywords=[
                str(keyword)
                for keyword in (item.get("keywords") or [])
                if str(keyword).strip()
            ],
            entities=[
                entity
                for entity in (item.get("entities") or [])
                if isinstance(entity, dict)
            ],
            image_uri=str(item.get("image_uri") or ""),
            html_uri=str(item.get("html_uri") or ""),
            image_path=str(item.get("image_path") or ""),
            html_path=str(item.get("html_path") or ""),
            extraction_status=str(item.get("extraction_status") or "loaded"),
            source_page_nums=source_page_nums,
        )
        if page_index > 0:
            assets_by_page.setdefault(page_index, []).append(asset)
    return assets_by_page


def load_scope_skeletons_artifact(path: Path) -> tuple[dict[str, Any], list[Any]]:
    """Load Stage3 ``skeletons.json`` envelope → (meta, SectionSkeleton list)."""
    from app.services.page_memory.skeleton_extractor import SectionSkeleton

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"scope skeletons artifact must be an object: {path}")
    raw_rows = data.get("skeletons")
    if not isinstance(raw_rows, list):
        raise ValueError(f"scope skeletons artifact missing skeletons[]: {path}")
    skeletons = [
        SectionSkeleton(
            section_path=str(item.get("section_path") or ""),
            title=str(item.get("title") or ""),
            level=int(item.get("level") or 0),
            start_page=int(item.get("start_page") or 0),
            end_page=int(item.get("end_page") or 0),
            parent_path=item.get("parent_path")
            if isinstance(item.get("parent_path"), str)
            else None,
            evidence=dict(item.get("evidence") or {})
            if isinstance(item.get("evidence"), dict)
            else {},
        )
        for item in raw_rows
        if isinstance(item, dict)
    ]
    start_page = int(data.get("start_page") or 0)
    end_page = int(data.get("end_page") or start_page)
    meta = {
        "scope_id": str(data.get("scope_id") or path.parent.name),
        "start_page": start_page,
        "end_page": end_page,
        "page_count": int(data.get("page_count") or max(end_page - start_page + 1, 0)),
        "strategy": str(data.get("strategy") or ""),
        "skeleton_count": int(data.get("skeleton_count") or len(skeletons)),
        "processing_pages": list(data.get("processing_pages") or []),
        "excluded_toc_pages": list(data.get("excluded_toc_pages") or []),
    }
    return meta, skeletons


def _scope_meta_from_dir(scope_dir: Path) -> dict[str, Any]:
    """Read coarse scope metadata from ``skeletons.json`` (or scope_id fallback)."""
    skel_path = scope_dir / "skeletons.json"
    if skel_path.exists():
        try:
            meta, _ = load_scope_skeletons_artifact(skel_path)
            return meta
        except (ValueError, json.JSONDecodeError, OSError):
            pass
    start = end = 0
    name = scope_dir.name
    if name.startswith("p") and "-" in name:
        try:
            left, right = name[1:].split("-", 1)
            start, end = int(left), int(right)
        except ValueError:
            start = end = 0
    return {
        "scope_id": name,
        "start_page": start,
        "end_page": end,
        "page_count": max(end - start + 1, 0) if start and end else 0,
        "strategy": "",
        "skeleton_count": 0,
    }


def load_locate_cache(locate_cache: Path) -> list[Any]:
    from app.services.page_memory.skeleton_extractor import SectionSkeleton

    raw = json.loads(locate_cache.read_text(encoding="utf-8"))
    return [
        SectionSkeleton(
            section_path=r["section_path"],
            title=r["title"],
            level=r["level"],
            start_page=r["start_page"],
            end_page=r["end_page"],
            parent_path=r.get("parent_path"),
            evidence=r.get("evidence", {}),
        )
        for r in raw
    ]


def _serialize_skeletons(skeletons: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "section_path": skel.section_path,
            "title": skel.title,
            "level": skel.level,
            "start_page": skel.start_page,
            "end_page": skel.end_page,
            "parent_path": skel.parent_path,
            "evidence": skel.evidence,
        }
        for skel in skeletons
    ]


# ── Coarse scope helpers ─────────────────────────────────────────────────────


def build_debug_coarse_scopes(
    *,
    skeletons: list[Any],
    filename: str,
    page_count: int,
    anatomy: Any | None = None,
) -> list[dict[str, Any]]:
    from app.services.page_memory._utils import build_hierarchy_scopes
    from toc_page_policy import TocPagePolicy

    policy = TocPagePolicy.from_anatomy(anatomy)
    scopes = build_hierarchy_scopes(
        skeletons=skeletons,
        filename=filename,
        page_count=page_count,
    )
    return [
        {
            "scope_id": scope.scope_id,
            "skeletons": scope.skeletons,
            "start_page": scope.start_page,
            "end_page": scope.end_page,
            "strategy": scope.strategy,
            "processing_pages": policy.filter_processing_pages(
                list(range(scope.start_page, scope.end_page + 1))
            ),
            "excluded_toc_pages": sorted(
                page
                for page in policy.pure_toc_pages
                if scope.start_page <= page <= scope.end_page
            ),
        }
        for scope in scopes
    ]


def add_scope_selection_args(parser: argparse.ArgumentParser) -> None:
    """Flags shared by stage 4/5/6 for picking one or more coarse scopes."""
    parser.add_argument(
        "--scope-id",
        default=None,
        help="Process one scope only (e.g. p14-23). Repeat via comma: p14-23,p38-41",
    )
    parser.add_argument(
        "--all-scopes",
        action="store_true",
        help="Process every scope under scopes/ (default when no selector is set)",
    )
    parser.add_argument(
        "--page-range",
        default=None,
        help="Select scope(s) overlapping this page range (e.g. 14-23 or 225)",
    )
    parser.add_argument(
        "--fat-only",
        action="store_true",
        help="Select the single largest scope by page span",
    )
    parser.add_argument(
        "--list-scopes",
        action="store_true",
        help="Print available scopes and exit",
    )


def list_scope_dirs(
    scopes_dir: Path,
    *,
    require_file: str = "skeletons.json",
    nonempty_json: bool = False,
) -> list[dict[str, Any]]:
    """Return scope metadata for directories that contain ``require_file``."""
    if not scopes_dir.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(scopes_dir.iterdir()):
        if not path.is_dir():
            continue
        required = path / require_file
        if not required.exists():
            continue
        if nonempty_json:
            text = required.read_text(encoding="utf-8").strip()
            if not text or text in {"[]", "{}", "null"}:
                continue
        info = _scope_meta_from_dir(path)
        start = int(info.get("start_page") or 0)
        end = int(info.get("end_page") or 0)
        rows.append(
            {
                "scope_id": path.name,
                "start_page": start,
                "end_page": end,
                "page_count": int(info.get("page_count") or max(end - start + 1, 0)),
                "skeleton_count": int(info.get("skeleton_count") or 0),
                "strategy": str(info.get("strategy") or ""),
                "path": path,
            }
        )
    return rows


def resolve_debug_scope_ids(
    *,
    scopes_dir: Path,
    scope_id: str | None = None,
    page_range: str | None = None,
    fat_only: bool = False,
    all_scopes: bool = False,
    list_scopes: bool = False,
    require_file: str = "skeletons.json",
    nonempty_json: bool = False,
) -> list[str]:
    """Resolve which scope directories to process; exit on list/validate errors."""
    available = list_scope_dirs(
        scopes_dir,
        require_file=require_file,
        nonempty_json=nonempty_json,
    )
    if list_scopes:
        if not available:
            logger.error("❌ No scopes with {} under {}", require_file, scopes_dir)
            raise SystemExit(1)
        logger.info("Available scopes ({}):", len(available))
        for row in available:
            logger.info(
                "  {}  p{}-{}  pages={}  skeletons={}  {}",
                row["scope_id"],
                row["start_page"],
                row["end_page"],
                row["page_count"],
                row["skeleton_count"],
                row["strategy"],
            )
        raise SystemExit(0)

    if not available:
        logger.error("❌ No scope directories with {} found under {}", require_file, scopes_dir)
        logger.error(
            "   Run Stage 3 first: uv run python scripts/page_memory/"
            "debug_pm_stage3_coarse_scope.py --file ..."
        )
        raise SystemExit(1)

    by_id = {row["scope_id"]: row for row in available}
    selected: list[str] = []

    if scope_id:
        requested = [part.strip() for part in str(scope_id).split(",") if part.strip()]
        missing = [sid for sid in requested if sid not in by_id]
        if missing:
            logger.error("❌ Unknown scope-id(s): {}", ", ".join(missing))
            logger.error(
                "   Available: {}",
                ", ".join(row["scope_id"] for row in available),
            )
            raise SystemExit(1)
        selected = requested
    elif fat_only:
        fattest = max(
            available,
            key=lambda row: int(row["end_page"]) - int(row["start_page"]),
        )
        selected = [fattest["scope_id"]]
        logger.info(
            "🎯 --fat-only → {}  p{}-{}",
            fattest["scope_id"],
            fattest["start_page"],
            fattest["end_page"],
        )
    elif page_range:
        parts = str(page_range).split("-")
        try:
            pr_start = int(parts[0])
            pr_end = int(parts[1]) if len(parts) > 1 else pr_start
        except ValueError:
            logger.error("❌ Invalid --page-range {!r}; expected e.g. 14-23", page_range)
            raise SystemExit(1) from None
        selected = [
            row["scope_id"]
            for row in available
            if row["start_page"] <= pr_end and row["end_page"] >= pr_start
        ]
        if not selected:
            logger.error(
                "❌ No scope overlaps --page-range {}-{} under {}",
                pr_start,
                pr_end,
                scopes_dir,
            )
            raise SystemExit(1)
        logger.info(
            "📄 --page-range {}-{} → {} scope(s): {}",
            pr_start,
            pr_end,
            len(selected),
            ", ".join(selected),
        )
    else:
        # Default: all scopes (``--all-scopes`` is documented as the same).
        selected = [row["scope_id"] for row in available]
        if all_scopes:
            logger.info("   --all-scopes: {} scopes", len(selected))

    return selected


# ── Require cache helper ─────────────────────────────────────────────────────


def require_file(path: Path, *, hint: str) -> None:
    """Abort with a clear message if a required cache file is missing."""
    if not path.exists():
        logger.error(f"❌ Required file not found: {path}")
        logger.error(f"   Hint: {hint}")
        raise SystemExit(1)
