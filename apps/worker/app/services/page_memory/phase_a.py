"""Phase A: coarse skeletons, frozen scope manifest, and coarse page rows."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from app.services.document_agent.pdf_text import coerce_page_text_cache
from app.services.document_agent.structure.toc_anchoring import pages_excluding_toc
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import PARSER_DF_COLUMNS
from app.services.page_memory._serialization import (
    derive_hierarchy_page_scope,
    scope_manifest,
    serialize_skeletons,
    write_json,
)
from app.services.page_memory._utils import (
    CoarseScope,
    build_hierarchy_scopes,
    is_toc_node,
    page_scope_info,
)
from app.services.page_memory.body_text_preview import build_body_text_preview
from app.services.page_memory.fine_hierarchy import (
    EndTitleAnchor,
    build_next_title_by_path,
)
from app.services.page_memory.skeleton_extractor import SectionSkeleton
from shared.services.chunks.path_segments import join_document_path


PHASE_B_DIR = "phase_b"
PHASE_B_BLACKBOARD_REL = f"{PHASE_B_DIR}/blackboard.json"
PHASE_B_MANIFEST_REL = f"{PHASE_B_DIR}/manifest.json"
PHASE_B_ANATOMY_REL = f"{PHASE_B_DIR}/anatomy.json"
PHASE_B_FULL_TEXT_REL = f"{PHASE_B_DIR}/page_full_text_cache.json"
PHASE_B_BODY_TEXT_REL = f"{PHASE_B_DIR}/page_body_text_cache.json"
PHASE_B_SOURCE_PDF_REL = "source.pdf"
PHASE_B_INPUT_RELS = (
    PHASE_B_SOURCE_PDF_REL,
    PHASE_B_BLACKBOARD_REL,
    PHASE_B_MANIFEST_REL,
    PHASE_B_ANATOMY_REL,
    PHASE_B_FULL_TEXT_REL,
    PHASE_B_BODY_TEXT_REL,
)
TEXT_ENRICHMENT_STATE = "text"
COARSE_ENRICHMENT_STATE = "coarse"


@dataclass(frozen=True)
class PhaseAResult:
    coarse_scopes: list[CoarseScope]
    next_title_by_path: dict[str, EndTitleAnchor]
    toc_pages: list[int]
    skeletons: list[SectionSkeleton]
    coarse_df: pd.DataFrame
    manifest: dict[str, Any]
    page_texts: dict[int, str]


def build_phase_a(
    *,
    profile: Any,
    filename: str,
    trace_recorder: Any | None = None,
) -> PhaseAResult:
    """Run through C4 skeletons, freeze scopes, and build coarse page rows."""
    from app.services.page_memory.skeleton_extractor import extract_section_skeletons

    page_texts = coerce_page_text_cache(getattr(profile, "page_full_text_cache", None))
    page_count = max(int(getattr(profile, "page_count", 0) or 0), 0)
    anatomy = getattr(profile, "anatomy", None)
    if page_count <= 0:
        empty = pd.DataFrame(columns=pd.Index(PARSER_DF_COLUMNS))
        return PhaseAResult(
            coarse_scopes=[],
            next_title_by_path={},
            toc_pages=[],
            skeletons=[],
            coarse_df=empty,
            manifest=_empty_manifest(filename=filename, page_count=page_count),
            page_texts=page_texts,
        )

    skeletons = (
        extract_section_skeletons(
            anatomy=anatomy,
            filename=filename,
            page_texts=page_texts,
        )
        if anatomy is not None
        else []
    )
    if not skeletons:
        skeletons = [
            SectionSkeleton(
                section_path=join_document_path([filename, "Root"]),
                level=1,
                start_page=1,
                end_page=page_count,
                title="Root",
                parent_path=filename,
                evidence={"source": "fallback_root", "reason": "no_hierarchy"},
            )
        ]

    next_title_by_path = build_next_title_by_path(skeletons)
    coarse_scopes = build_hierarchy_scopes(
        skeletons=skeletons,
        filename=filename,
        page_count=page_count,
    )
    toc_result = getattr(anatomy, "toc_result", None) if anatomy is not None else None
    toc_pages = sorted(
        {int(page) for page in (getattr(toc_result, "toc_pages", None) or [])}
    )
    manifest = build_phase_a_manifest(
        filename=filename,
        page_count=page_count,
        skeletons=skeletons,
        coarse_scopes=coarse_scopes,
        next_title_by_path=next_title_by_path,
        toc_pages=toc_pages,
    )
    _record_trace_stage(
        trace_recorder,
        "C4.skeleton",
        page_info={
            "document_page_count": page_count,
            "coarse_scope": page_scope_info(
                pages_excluding_toc(
                    derive_hierarchy_page_scope(
                        skeletons=skeletons,
                        page_count=page_count,
                    ),
                    toc_pages,
                )
            ),
        },
        variables={
            "sections": [
                {
                    "path": item.section_path,
                    "level": item.level,
                    "start_page": item.start_page,
                    "end_page": item.end_page,
                }
                for item in skeletons
            ],
            "scope_count": len(coarse_scopes),
            "scopes": [entry for entry in manifest["scopes"]],
        },
    )
    coarse_df = build_coarse_dataframe(
        coarse_scopes=coarse_scopes,
        page_texts=page_texts,
        body_text_by_page=coerce_page_text_cache(
            getattr(profile, "page_body_text_cache", None)
        ),
        toc_pages=toc_pages,
        page_count=page_count,
        filename=filename,
    )
    return PhaseAResult(
        coarse_scopes=coarse_scopes,
        next_title_by_path=next_title_by_path,
        toc_pages=toc_pages,
        skeletons=skeletons,
        coarse_df=coarse_df,
        manifest=manifest,
        page_texts=page_texts,
    )


def build_phase_a_manifest(
    *,
    filename: str,
    page_count: int,
    skeletons: list[SectionSkeleton],
    coarse_scopes: list[CoarseScope],
    next_title_by_path: dict[str, EndTitleAnchor],
    toc_pages: list[int],
) -> dict[str, Any]:
    scope_entries: list[dict[str, Any]] = []
    for scope in coarse_scopes:
        base = scope_manifest(
            scope_id=scope.scope_id,
            skeletons=scope.skeletons,
            page_count=page_count,
            strategy=scope.strategy,
        )
        skeleton_paths = [
            str(getattr(item, "section_path", "") or "") for item in scope.skeletons
        ]
        processing_pages = (
            []
            if scope.skeletons and all(is_toc_node(item) for item in scope.skeletons)
            else pages_excluding_toc(
                derive_hierarchy_page_scope(
                    skeletons=scope.skeletons,
                    page_count=page_count,
                ),
                toc_pages,
            )
        )
        excluded_toc_pages = sorted(
            page for page in toc_pages if scope.start_page <= page <= scope.end_page
        )
        scope_entries.append(
            {
                **base,
                "start_page": scope.start_page,
                "end_page": scope.end_page,
                "skeletons": serialize_skeletons(scope.skeletons),
                "next_title_slice": {
                    path: next_title_by_path.get(path)
                    for path in skeleton_paths
                    if path in next_title_by_path
                },
                "toc_pages": list(toc_pages),
                "processing_pages": processing_pages,
                "excluded_toc_pages": excluded_toc_pages,
            }
        )
    return {
        "filename": filename,
        "page_count": page_count,
        "toc_pages": list(toc_pages),
        "next_title_by_path": dict(next_title_by_path),
        "skeletons": serialize_skeletons(skeletons),
        "scopes": scope_entries,
    }


def build_coarse_dataframe(
    *,
    coarse_scopes: list[CoarseScope],
    page_texts: dict[int, str],
    body_text_by_page: dict[int, str],
    toc_pages: list[int],
    page_count: int,
    filename: str,
) -> pd.DataFrame:
    """One coarse page row per scoped skeleton: raw cache text, no VLM."""
    rows: list[dict[str, Any]] = []
    for scope in coarse_scopes:
        for skeleton in scope.skeletons:
            pages = _skeleton_pages(
                skeleton=skeleton,
                page_count=page_count,
                toc_pages=toc_pages,
            )
            content = "\n\n".join(page_texts.get(page, "") for page in pages).strip()
            path = str(skeleton.section_path)
            rows.append(
                {
                    "content": content,
                    "path": path,
                    "type": "page",
                    "length": len(content),
                    "keywords": "",
                    "summary": "",
                    "know_id": f"node_{gen_str_codes(f'{filename}::{path}')}",
                    "tokens": "",
                    "connectto": "",
                    "addtime": get_str_time(),
                    "page_nums": ",".join(str(page) for page in pages),
                    "extra_metadata": {
                        "scope_id": scope.scope_id,
                        "enrichment_state": COARSE_ENRICHMENT_STATE,
                        "body_text_preview": build_body_text_preview(
                            body_text_by_page.get(page, "") for page in pages
                        ),
                    },
                }
            )
    return pd.DataFrame(rows, columns=pd.Index(PARSER_DF_COLUMNS))


def build_coarse_text_dataframe(
    *,
    page_texts: dict[int, str],
    body_text_by_page: dict[int, str],
    page_count: int,
    filename: str,
    image_path_by_page: dict[int, str] | None = None,
    embeddings_by_page: dict[int, list[float]] | None = None,
    output_dir: str | None = None,
) -> pd.DataFrame:
    """M0 baseline: one flat per-page row (path=filename/p{n}, enrichment_state='text').

    Every PDF carries PyMuPDF band text; when page images/embeddings are supplied
    the row also carries ``page_assets`` and ``_page_embedding``.
    """
    from app.services.page_memory.node_assembler import _build_page_citation_assets

    rows: list[dict[str, Any]] = []
    for page in range(1, max(int(page_count or 0), 0) + 1):
        content = str(page_texts.get(page, "") or "").strip()
        scope_id = f"p{page}-{page}"
        path = join_document_path([filename, f"p{page}"])
        extra_metadata: dict[str, Any] = {
            "scope_id": scope_id,
            "enrichment_state": TEXT_ENRICHMENT_STATE,
            "body_text_preview": build_body_text_preview(
                [body_text_by_page.get(page, "")]
            ),
        }
        if image_path_by_page and output_dir:
            page_assets = _build_page_citation_assets(
                pages=[page],
                image_path_by_page=image_path_by_page,
                output_dir=output_dir,
            )
            if page_assets:
                extra_metadata["page_assets"] = page_assets
        if image_path_by_page is not None and embeddings_by_page is None:
            extra_metadata["embedding_status"] = "pending"
        if embeddings_by_page is not None:
            embedding = embeddings_by_page.get(page)
            extra_metadata["embedding_status"] = "complete" if embedding else "missing"
            if isinstance(embedding, list) and embedding:
                extra_metadata["_page_embedding"] = [float(val) for val in embedding]
        rows.append(
            {
                "content": content,
                "path": path,
                "type": "page",
                "length": len(content),
                "keywords": "",
                "summary": "",
                "know_id": f"node_{gen_str_codes(f'{filename}::{path}')}",
                "tokens": "",
                "connectto": "",
                "addtime": get_str_time(),
                "page_nums": str(page),
                "extra_metadata": extra_metadata,
            }
        )
    return pd.DataFrame(rows, columns=pd.Index(PARSER_DF_COLUMNS))


def persist_m0_inputs(
    *,
    output_dir: str,
    profile: Any,
) -> list[str]:
    """Persist M0 blackboard snapshot + text caches under ``output_dir/phase_b/``."""
    from app.services.document_agent.pdf_text import PageTextBands

    root = Path(output_dir)
    blackboard = getattr(profile, "blackboard", None)
    if blackboard is not None:
        doc_profile = getattr(blackboard, "document_profile", None)
        bands_raw = getattr(blackboard, "page_full_text_cache", None) or {}
        bands_json = {
            str(page): PageTextBands.from_any(val).to_dict()
            for page, val in dict(bands_raw).items()
        }
        snapshot = {
            "version": "1.0",
            "page_count": int(getattr(blackboard, "page_count", 0) or 0),
            "document_profile": (
                doc_profile.to_dict() if doc_profile is not None else None
            ),
            "page_features": [
                feature.to_dict()
                for feature in (getattr(blackboard, "page_features", None) or [])
            ],
            "page_labels": [
                label.to_dict()
                for label in (getattr(blackboard, "page_labels", None) or [])
            ],
            "doc_stats": dict(getattr(blackboard, "doc_stats", None) or {}),
            "global_signals": dict(getattr(blackboard, "global_signals", None) or {}),
            "page_full_text_bands": bands_json,
        }
        write_json(root / PHASE_B_BLACKBOARD_REL, snapshot)
    write_json(
        root / PHASE_B_FULL_TEXT_REL,
        _jsonable_page_map(getattr(profile, "page_full_text_cache", None)),
    )
    write_json(
        root / PHASE_B_BODY_TEXT_REL,
        _jsonable_page_map(getattr(profile, "page_body_text_cache", None)),
    )
    return existing_phase_b_input_refs(output_dir)


def persist_phase_b_inputs(
    *,
    output_dir: str,
    profile: Any,
    manifest: dict[str, Any],
) -> list[str]:
    """Write Phase B inputs under ``output_dir`` and return existing relative paths."""
    root = Path(output_dir)
    write_json(root / PHASE_B_MANIFEST_REL, manifest)
    write_json(
        root / PHASE_B_FULL_TEXT_REL,
        _jsonable_page_map(getattr(profile, "page_full_text_cache", None)),
    )
    write_json(
        root / PHASE_B_BODY_TEXT_REL,
        _jsonable_page_map(getattr(profile, "page_body_text_cache", None)),
    )
    anatomy = getattr(profile, "anatomy", None)
    if anatomy is not None and hasattr(anatomy, "to_dict"):
        write_json(root / PHASE_B_ANATOMY_REL, anatomy.to_dict())
    return existing_phase_b_input_refs(output_dir)


def existing_phase_b_input_refs(output_dir: str) -> list[str]:
    return [
        relative
        for relative in PHASE_B_INPUT_RELS
        if os.path.isfile(os.path.join(output_dir, relative))
    ]


def scopes_from_phase_a_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    scopes: list[dict[str, Any]] = []
    for entry in manifest.get("scopes") or []:
        if not isinstance(entry, dict):
            continue
        scopes.append(
            {
                "scope_id": str(entry["scope_id"]),
                "skeletons": skeletons_from_rows(list(entry.get("skeletons") or [])),
                "start_page": int(entry["start_page"]),
                "end_page": int(entry["end_page"]),
                "strategy": str(entry.get("strategy") or ""),
                "processing_pages": [
                    int(page) for page in (entry.get("processing_pages") or [])
                ],
                "excluded_toc_pages": [
                    int(page) for page in (entry.get("excluded_toc_pages") or [])
                ],
            }
        )
    return scopes


def skeletons_from_phase_a_manifest(manifest: dict[str, Any]) -> list[SectionSkeleton]:
    return skeletons_from_rows(list(manifest.get("skeletons") or []))


def skeletons_from_rows(rows: list[Any]) -> list[SectionSkeleton]:
    skeletons: list[SectionSkeleton] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        skeletons.append(
            SectionSkeleton(
                section_path=str(row["section_path"]),
                title=str(row["title"]),
                level=int(row["level"]),
                start_page=int(row["start_page"]),
                end_page=int(row["end_page"]),
                parent_path=row.get("parent_path"),
                evidence=dict(row.get("evidence") or {}),
            )
        )
    return skeletons


def next_title_by_path_from_manifest(
    manifest: dict[str, Any],
) -> dict[str, EndTitleAnchor]:
    raw = manifest.get("next_title_by_path")
    if not isinstance(raw, dict):
        raise ValueError("phase A manifest is missing next_title_by_path")
    next_map: dict[str, EndTitleAnchor] = {}
    for path, title in raw.items():
        if title is None:
            next_map[str(path)] = None
        elif isinstance(title, list | tuple):
            items = [str(x).strip() for x in title if str(x).strip()]
            next_map[str(path)] = (
                None if not items else items[0] if len(items) == 1 else items
            )
        else:
            next_map[str(path)] = str(title)
    skeleton_rows = manifest.get("skeletons")
    if isinstance(skeleton_rows, list) and skeleton_rows:
        recomputed = build_next_title_by_path(skeletons_from_rows(skeleton_rows))
        for path, anchors in recomputed.items():
            if anchors is not None:
                next_map[path] = anchors
    return next_map


def require_phase_a_manifest(stage2: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(stage2, dict):
        raise ValueError("pipeline state is missing stages.stage2")
    manifest = stage2.get("scope_manifest")
    if not isinstance(manifest, dict) or "scopes" not in manifest:
        raise ValueError("pipeline state is missing stages.stage2.scope_manifest")
    return manifest


def _empty_manifest(*, filename: str, page_count: int) -> dict[str, Any]:
    return {
        "filename": filename,
        "page_count": page_count,
        "toc_pages": [],
        "next_title_by_path": {},
        "skeletons": [],
        "scopes": [],
    }


def _skeleton_pages(
    *,
    skeleton: SectionSkeleton,
    page_count: int,
    toc_pages: list[int],
) -> list[int]:
    start_page = max(1, int(getattr(skeleton, "start_page", 1) or 1))
    end_page = min(
        page_count,
        int(getattr(skeleton, "end_page", start_page) or start_page),
    )
    if end_page < start_page or page_count <= 0:
        return []
    pages = list(range(start_page, end_page + 1))
    if is_toc_node(skeleton):
        claimed = {int(page) for page in toc_pages}
        return [page for page in pages if page in claimed]
    return pages_excluding_toc(pages, toc_pages)


def _jsonable_page_map(raw: Any) -> dict[str, str]:
    coerced = coerce_page_text_cache(raw)
    return {str(page): text for page, text in sorted(coerced.items())}


def _record_trace_stage(
    trace_recorder: Any | None,
    stage: str,
    *,
    page_info: dict[str, Any],
    variables: dict[str, Any],
) -> None:
    if trace_recorder is None or not hasattr(trace_recorder, "record_stage"):
        return
    trace_recorder.record_stage(stage, page_info=page_info, variables=variables)
