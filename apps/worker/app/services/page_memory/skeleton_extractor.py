"""Build page-memory section skeletons from profile-time anatomy."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.document_agent.manifest import (
    H1Candidate,
    PageAnatomyMap,
    Shard,
    ToolContext,
)
from app.services.document_agent.structure.page_locate_agent import (
    PageLocateResidualAgent,
)
from app.services.document_agent.structure.hierarchy_locator import (
    ResolvedHierarchyRange,
    TitleNode,
    extract_toc_nodes,
    resolve_hierarchy_page_ranges,
)
from app.services.document_parser.structure.body_boundary import (
    clean_toc_title,
    normalize_heading_text,
)
from loguru import logger

_FRONT_TOC_REGION_GAP_PAGES = 5


@dataclass(frozen=True)
class SectionSkeleton:
    section_path: str
    level: int
    start_page: int
    end_page: int
    title: str
    parent_path: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_section_skeletons(
    *,
    anatomy: PageAnatomyMap | Any | None,
    filename: str,
    page_texts: dict[int, str],
    ctx: ToolContext | None = None,
    hierarchy_nodes: list[TitleNode] | None = None,
) -> list[SectionSkeleton]:
    """Convert PageAnatomyMap hierarchy evidence into leaf section skeletons."""
    page_count = _page_count(anatomy)
    root_path = f"{filename}/Root"
    if page_count <= 0:
        return [_root_skeleton(root_path=root_path, filename=filename, page_count=0)]

    toc_selection: dict[str, Any] = {}
    if hierarchy_nodes:
        nodes = hierarchy_nodes
    else:
        toc_hierarchies, toc_selection = _select_global_toc_hierarchies(
            anatomy=anatomy,
            filename=filename,
        )
        toc_nodes = extract_toc_nodes(toc_hierarchies)
        nodes = toc_nodes or _h1_nodes(anatomy)
    if not nodes:
        return [
            _root_skeleton(
                root_path=root_path,
                filename=filename,
                page_count=page_count,
                reason="no_hierarchy",
            )
        ]

    body_pages = _body_pages(anatomy=anatomy, page_count=page_count)
    offset_hint = _estimate_page_offset(nodes=nodes, anatomy=anatomy)
    locate_result = PageLocateResidualAgent(
        ctx=ctx,
        page_texts=page_texts,
        body_pages=body_pages,
        page_count=page_count,
        page_offset_hint=offset_hint,
    ).prepare(nodes)
    ranges = resolve_hierarchy_page_ranges(
        locate_result.nodes,
        page_count=page_count,
        page_texts=page_texts,
        body_pages=body_pages,
        page_offset_hint=offset_hint,
        match_overrides=locate_result.match_overrides,
    )
    if not ranges:
        return [
            _root_skeleton(
                root_path=root_path,
                filename=filename,
                page_count=page_count,
                reason="unresolved_hierarchy",
            )
        ]

    skeletons = [
        _range_to_skeleton(
            item,
            filename=filename,
            page_count=page_count,
            shards=_shards(anatomy),
            locate_summary=locate_result.summary,
            toc_selection=toc_selection,
        )
        for item in ranges
    ]
    _log_unlocated_title_warnings(filename=filename, skeletons=skeletons)
    return skeletons


def _range_to_skeleton(
    item: ResolvedHierarchyRange,
    *,
    filename: str,
    page_count: int,
    shards: list[Shard],
    locate_summary: dict[str, Any],
    toc_selection: dict[str, Any],
) -> SectionSkeleton:
    start_page = _clamp_page(item.start_page, page_count)
    end_page = _clamp_to_shard(
        start_page=start_page,
        end_page=_clamp_page(item.end_page, page_count),
        shards=shards,
    )
    path_titles = [clean_toc_title(title) or title for title in item.path_titles]
    section_path = "/".join([filename, *path_titles])
    parent_path = "/".join([filename, *path_titles[:-1]]) if len(path_titles) > 1 else filename
    evidence = {
        **item.evidence,
        "resolver": "hierarchy_locator",
        "path_titles": path_titles,
        "page_locate_summary": locate_summary,
    }
    if toc_selection:
        evidence["toc_selection"] = toc_selection
    if end_page != item.end_page:
        evidence["shard_clamped"] = True
    return SectionSkeleton(
        section_path=section_path,
        level=item.level,
        start_page=start_page,
        end_page=end_page,
        title=item.title,
        parent_path=parent_path,
        evidence=evidence,
    )


def _root_skeleton(
    *,
    root_path: str,
    filename: str,
    page_count: int,
    reason: str = "no_pages",
) -> SectionSkeleton:
    end_page = max(page_count, 1)
    return SectionSkeleton(
        section_path=root_path,
        level=1,
        start_page=1,
        end_page=end_page,
        title="Root",
        parent_path=filename,
        evidence={"source": "fallback_root", "reason": reason},
    )


def _page_count(anatomy: Any | None) -> int:
    return max(int(getattr(anatomy, "page_count", 0) or 0), 0)


def _toc_hierarchies(anatomy: Any | None) -> list[dict[str, Any]] | None:
    return getattr(anatomy, "toc_hierarchies", None) if anatomy is not None else None


def _select_global_toc_hierarchies(
    *,
    anatomy: Any | None,
    filename: str,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Keep the front/global TOC cluster and skip later embedded TOCs.

    Profile-time TOC extraction can find local TOCs inside a long document
    (for example, an embedded standard with its own English outline). Page
    memory C4 currently emits a document-level skeleton, so later page-based
    TOC regions must not be concatenated as root siblings.
    """
    hierarchies = list(_toc_hierarchies(anatomy) or [])
    if len(hierarchies) <= 1:
        return (hierarchies or None), {}

    page_based = [
        hierarchy
        for hierarchy in hierarchies
        if hierarchy.get("toc_range_unit") == "page" and _toc_range_start(hierarchy) is not None
    ]
    if not page_based or len(page_based) != len(hierarchies):
        return hierarchies, {}

    sorted_items = sorted(enumerate(hierarchies), key=lambda item: _toc_range_start(item[1]) or 0)
    selected_indices: set[int] = set()
    skipped: list[dict[str, Any]] = []
    cluster_end: int | None = None

    for original_index, hierarchy in sorted_items:
        start = _toc_range_start(hierarchy)
        end = _toc_range_end(hierarchy)
        if start is None or end is None:
            selected_indices.add(original_index)
            continue
        if cluster_end is None:
            selected_indices.add(original_index)
            cluster_end = end
            continue
        if start <= cluster_end + _FRONT_TOC_REGION_GAP_PAGES:
            selected_indices.add(original_index)
            cluster_end = max(cluster_end, end)
            continue
        skipped.append(
            {
                "index": original_index,
                "toc_range": [start, end],
                "scan_range": hierarchy.get("scan_range"),
                "reason": "embedded_toc_region_outside_front_cluster",
            }
        )

    selected = [
        hierarchy
        for index, hierarchy in enumerate(hierarchies)
        if index in selected_indices
    ]
    if skipped:
        logger.warning(
            "[page_memory.skeleton] skipped embedded toc regions filename={} skipped={}",
            filename,
            skipped,
        )
    summary = {
        "strategy": "front_page_toc_cluster",
        "input_count": len(hierarchies),
        "selected_count": len(selected),
        "skipped_count": len(skipped),
        "skipped": skipped,
    }
    return (selected or None), summary


def _toc_range_start(hierarchy: dict[str, Any]) -> int | None:
    toc_range = hierarchy.get("toc_range")
    if not isinstance(toc_range, (list, tuple)) or not toc_range:
        return None
    try:
        return int(toc_range[0])
    except (TypeError, ValueError):
        return None


def _toc_range_end(hierarchy: dict[str, Any]) -> int | None:
    toc_range = hierarchy.get("toc_range")
    if not isinstance(toc_range, (list, tuple)) or not toc_range:
        return None
    try:
        return int(toc_range[-1])
    except (TypeError, ValueError):
        return None


def _h1_nodes(anatomy: Any | None) -> list[TitleNode]:
    h1_result = getattr(anatomy, "h1_result", None)
    candidates: list[H1Candidate] = list(getattr(h1_result, "h1_candidates", []) or [])
    nodes: list[TitleNode] = []
    for candidate in sorted(candidates, key=lambda item: item.page):
        title = clean_toc_title(candidate.title) or normalize_heading_text(candidate.title)
        if not title:
            continue
        nodes.append(TitleNode(title=title, level=1, physical_page_hint=candidate.page))
    return nodes


def _body_pages(*, anatomy: Any | None, page_count: int) -> list[int]:
    excluded: set[int] = set()
    toc_result = getattr(anatomy, "toc_result", None)
    excluded.update(int(page) for page in getattr(toc_result, "toc_pages", []) or [])
    return [page for page in range(1, page_count + 1) if page not in excluded]


def _estimate_page_offset(*, nodes: list[TitleNode], anatomy: Any | None) -> int | None:
    printed_by_title: dict[str, int] = {}
    for node in _walk_nodes(nodes):
        if node.printed_page is None:
            continue
        printed_by_title[_title_key(node.title)] = node.printed_page

    offsets: list[int] = []
    h1_result = getattr(anatomy, "h1_result", None)
    for candidate in getattr(h1_result, "h1_candidates", []) or []:
        printed_page = printed_by_title.get(_title_key(candidate.title))
        if printed_page is not None:
            offsets.append(int(candidate.page) - printed_page)
    if not offsets:
        return None
    offsets.sort()
    return offsets[len(offsets) // 2]


def _walk_nodes(nodes: list[TitleNode]) -> list[TitleNode]:
    walked: list[TitleNode] = []
    for node in nodes:
        walked.append(node)
        walked.extend(_walk_nodes(node.children))
    return walked


def _title_key(title: str) -> str:
    return normalize_heading_text(clean_toc_title(title) or title).casefold()


def _shards(anatomy: Any | None) -> list[Shard]:
    shard_plan = getattr(anatomy, "shard_plan", None)
    return list(getattr(shard_plan, "shards", []) or [])


def _clamp_to_shard(*, start_page: int, end_page: int, shards: list[Shard]) -> int:
    for shard in shards:
        if shard.page_start <= start_page <= shard.page_end:
            return min(end_page, shard.page_end)
    return end_page


def _clamp_page(page: int, page_count: int) -> int:
    return min(max(page, 1), max(page_count, 1))


def _log_unlocated_title_warnings(
    *,
    filename: str,
    skeletons: list[SectionSkeleton],
) -> None:
    for skeleton in skeletons:
        for warning in skeleton.evidence.get("warnings", []) or []:
            logger.warning(
                "[page_memory.skeleton] title unlocated filename={} title={!r} "
                "assigned_range={} parent_scope={} path_titles={}",
                filename,
                warning.get("title"),
                warning.get("assigned_range"),
                warning.get("parent_scope"),
                warning.get("path_titles"),
            )
