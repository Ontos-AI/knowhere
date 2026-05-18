"""
summary_builder: Bottom-up recursive summarization for document navigation.

Reads doc_nav.json + chunks.json for a file and generates ``summary`` fields
at every intermediate node via LLM aggregation.
The enriched doc_nav.json is written back to disk.

Usage (standalone):
    from app.services.connect_builder.summary_builder import enrich_doc_nav_summaries
    enrich_doc_nav_summaries(document_workspace_dir, source_file="report.pdf")
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from openai.types.chat import ChatCompletionMessageParam


# ─── Constants ────────────────────────────────────────────────────────────────

# Summary output max length for recursive LLM aggregation (≤100 chars per node)
SUMMARY_MAX_LEN = 100
# Navigation top-summary budget measured in semantic tokens (count_cn_en)
NAVIGATION_TOP_SUMMARY_MAX_TOKENS = 200
# TODO: revisit this cap after we collect more real-world prompt/token budget data.
NON_LLM_TOP_SUMMARY_MAX_SECTIONS = 20
NON_LLM_TOP_SUMMARY_MAX_DEPTH = 2


# ─── LLM Interface ───────────────────────────────────────────────────────────


def _llm_summarize(snippets_text: str, node_name: str, max_tokens: int = 100) -> str:
    """
    Call LLM to produce a concise summary from aggregated child snippets.

    Returns plain text summary, or "" on failure.
    """
    try:
        from shared.services.ai.prompt_service import build_prompt, _detect_text_language
        from shared.services.ai.openai_compatible_client_sync import get_openai_client

        # Deterministic language lock — see prompt_service._language_directive
        detected_lang = _detect_text_language(snippets_text)
        prompt, temperature, top_p, _prompt_max_tokens = build_prompt(
            task="file-summary",
            texts=snippets_text,
            query="",
            paras={
                "max_tokens": max_tokens,
                "node_name": node_name,
                "lang": detected_lang,
            },
        )
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": "you are a helpful assistant"},
            {"role": "user", "content": prompt},
        ]
        resp = get_openai_client().chat_completion(
            messages=messages,
            timeout=60,
            max_tokens=max_tokens,
        )
        if resp is None:
            return ""
        if isinstance(resp, str):
            stripped = resp.strip()
            if stripped.lower() in ("null", "none"):
                return ""
            return stripped
        return ""
    except Exception as e:
        logger.warning(f"LLM summary failed for '{node_name}': {e}")
        return ""



#
# Uses explicit children arrays:
#   [{"title": "Section A", "summary": "...", "children": [{"title": "SubA1", ...}]}]
# ═══════════════════════════════════════════════════════════════════════════════


DOC_NAV_FILENAME = "doc_nav.json"


def _load_doc_nav(file_dir: str) -> Optional[Dict[str, Any]]:
    """Load doc_nav.json from a parsed file directory, return None if absent."""
    path = os.path.join(file_dir, DOC_NAV_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read {DOC_NAV_FILENAME}: dir={file_dir}, error={e}")
        return None


def _save_doc_nav(file_dir: str, doc_nav: Dict[str, Any]) -> None:
    """Write doc_nav.json back to disk."""
    path = os.path.join(file_dir, DOC_NAV_FILENAME)
    os.makedirs(file_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc_nav, f, ensure_ascii=False, indent=2)


def ensure_doc_nav_json(
    file_dir: str,
    chunks: List[Dict[str, Any]],
    source_file_name: str = "",
    *,
    overwrite: bool = False,
) -> str:
    """Materialize ``doc_nav.json`` from chunks when the parser did not emit one."""
    nav_path = os.path.join(file_dir, DOC_NAV_FILENAME)
    if os.path.exists(nav_path) and not overwrite:
        return nav_path

    from shared.services.storage.zip_result_schema import ZipResultSchemaBuilder

    doc_nav = ZipResultSchemaBuilder().build_doc_nav(chunks, source_file_name)

    _save_doc_nav(file_dir, doc_nav)
    return nav_path


# ─── Recursive summarization on doc_nav sections ─────────────────────────────


def _recursive_summarize_nav(
    node: Dict[str, Any],
    use_llm: bool = True,
    is_top_level: bool = False,
) -> str:
    """Bottom-up recursive summarization on a doc_nav section node.

    Operates on the children-array tree structure of doc_nav.json.

    For each node:
    - Leaf (children==[]) → keep existing summary (set during ZIP creation).
    - Non-leaf → recursively summarize children, then aggregate.

    Writes summary in-place into ``node["summary"]``.
    Returns the summary string.
    """
    children = node.get("children", [])
    title = node.get("title", "")

    if not children:
        # Leaf node — keep existing summary
        existing = (node.get("summary") or "").strip()
        if existing:
            node["summary"] = existing
        return node.get("summary", "")

    # Recurse into children
    child_summaries: List[Tuple[str, str]] = []
    for child in children:
        child_summary = _recursive_summarize_nav(child, use_llm, is_top_level=False)
        if child_summary:
            child_summaries.append((child.get("title", ""), child_summary))

    if not child_summaries:
        return node.get("summary", "")

    # Aggregate child summaries without hard truncation
    aggregated_parts = []
    for name, summary in child_summaries:
        aggregated_parts.append(f"[{name}] {summary}")

    aggregated_text = "\n".join(aggregated_parts)

    max_len = NAVIGATION_TOP_SUMMARY_MAX_TOKENS if is_top_level else SUMMARY_MAX_LEN

    if len(child_summaries) <= 1 and not is_top_level:
        result = child_summaries[0][1]
    else:
        if is_top_level and not use_llm:
            titles = [name for name, _ in child_summaries if name.lower() != "root"]
        else:
            titles = [name for name, _ in child_summaries]

        enum_prefix = "This document includes: " if is_top_level else "This section covers: "
        title_enum = enum_prefix + ", ".join(titles)

        if not use_llm:
            result = title_enum
        else:
            total_len = sum(len(s) for _, s in child_summaries)
            if total_len > SUMMARY_MAX_LEN:
                result = _llm_summarize(aggregated_text, title, max_tokens=max_len)
                if not result:
                    result = title_enum
            else:
                result = title_enum

    node["summary"] = result
    return result


def _doc_nav_has_enriched_summaries(doc_nav: Dict[str, Any]) -> bool:
    """Check if enrichment has already been run on this doc_nav.

    Aligned with original enrichment logic:
    In doc_nav.json, leaf nodes already have summary from ZIP creation,
    so only non-leaf (parent) summaries are set by enrichment.
    We recursively check that ALL non-leaf nodes across all depths
    have a non-empty summary — if any is missing, enrichment is incomplete.
    """
    def _check_sections(sections: List[Dict[str, Any]]) -> bool:
        """Returns True if all non-leaf nodes in sections have summaries."""
        for section in sections:
            children = section.get("children", [])
            if not children:
                continue
            # This is a non-leaf node — must have summary from enrichment
            if not section.get("summary"):
                return False
            # Recurse into children to check deeper non-leaf nodes
            if not _check_sections(children):
                return False
        return True

    sections = doc_nav.get("sections", [])
    if not sections:
        return False
    # Must have at least one non-leaf to be considered enriched
    has_non_leaf = any(s.get("children") for s in sections)
    if not has_non_leaf:
        return False
    return _check_sections(sections)


def _build_nav_top_summary(
    doc_nav: Dict[str, Any], 
    use_llm: bool = True
) -> str:
    """Build navigation-facing top summary from enriched doc_nav.json.

    Strategy:
    Treat all sections as children of a virtual Document node and recursively summarize.
    """
    sections = doc_nav.get("sections", [])
    if not sections:
        return ""


    virtual_doc_node = {
        "title": "Document Overview",
        "children": sections
    }
    
    top_summary = _recursive_summarize_nav(
        virtual_doc_node, 
        use_llm=use_llm, 
        is_top_level=True
    )
    
    return top_summary


def enrich_doc_nav_summaries(
    document_workspace_dir: str,
    source_file: Optional[str] = None,
    force: bool = False,
    use_llm: bool = True,
) -> Dict[str, str]:
    """Enrich doc_nav.json with bottom-up recursive summaries.

    Args:
        document_workspace_dir: Absolute path to the temporary document workspace.
        source_file: If given, only process this file. Otherwise process all.
        force: If True, regenerate even if summaries already exist.
        use_llm: If True, use LLM for multi-child aggregation.

    Returns:
        Dict mapping file_name → top-level summary string.
    """
    results: Dict[str, str] = {}
    mode_label = "LLM" if use_llm else "title-concat"

    if source_file:
        targets = [source_file]
    else:
        targets = [
            entry
            for entry in os.listdir(document_workspace_dir)
            if os.path.isdir(os.path.join(document_workspace_dir, entry))
            and not entry.startswith(".")
        ]

    for file_name in targets:
        file_dir = os.path.join(document_workspace_dir, file_name)
        doc_nav = _load_doc_nav(file_dir)
        if doc_nav is None:
            logger.debug(f"No {DOC_NAV_FILENAME} for {file_name}, skipping")
            continue

        if not force and _doc_nav_has_enriched_summaries(doc_nav):
            logger.debug(f"Summaries already exist in {DOC_NAV_FILENAME} for {file_name}, skipping")
            results[file_name] = _build_nav_top_summary(doc_nav, use_llm=use_llm)
            continue

        logger.info(
            f"📝 Enriching {DOC_NAV_FILENAME} summaries for {file_name} "
            f"(mode={mode_label})"
        )

        # Recursively summarize each top-level section
        for section in doc_nav.get("sections", []):
            _recursive_summarize_nav(section, use_llm=use_llm)

        _save_doc_nav(file_dir, doc_nav)
        logger.info(f"✅ doc_nav summaries saved for {file_name}")

        top_summary = _build_nav_top_summary(doc_nav, use_llm=use_llm)
        results[file_name] = top_summary

    return results


def load_nav_top_summary(file_dir: str, file_name: str = "") -> str:
    """Load doc_nav.json and extract the navigation top summary."""
    doc_nav = _load_doc_nav(file_dir)
    if doc_nav is not None:
        return _build_nav_top_summary(doc_nav, use_llm=False)
    return ""


def build_section_summary_lookup(file_dir: str) -> Dict[str, str]:
    """Build a flat {section_path: summary} dict from all nodes in doc_nav.json.

    Keys use the DocumentSection.section_path format produced by
    ``section_path_from_chunk_path`` (strips namespace + filename prefix,
    joins remaining parts with ``" / "``).

    Traverses the full section tree at all depths.  Used by the publication
    pipeline to backfill DocumentSection.summary rows.

    Args:
        file_dir: Absolute path to the file-level directory
                  inside the task-scoped parse workspace.

    Returns:
        Dict mapping section_path → summary string (empty dict on any error).
    """
    from shared.services.retrieval.search.lexical_text import section_path_from_chunk_path

    doc_nav = _load_doc_nav(file_dir)
    if doc_nav is None:
        return {}

    lookup: Dict[str, str] = {}

    def _walk(node: Dict[str, Any]) -> None:
        nav_path = node.get("path", "")
        summary = node.get("summary", "")
        if nav_path and summary:
            section_path = section_path_from_chunk_path(nav_path)
            if section_path and section_path != "Root":
                lookup[section_path] = summary
        for child in node.get("children", []):
            _walk(child)

    for section in doc_nav.get("sections", []):
        _walk(section)

    # Populate Root with the document-level top_summary (tree preview).
    # This mirrors GraphNode.properties.top_summary and ensures the
    # DocumentSection Root row has a summary for data completeness.
    if "Root" not in lookup:
        top_summary = _build_nav_top_summary(doc_nav, use_llm=False)
        if top_summary:
            lookup["Root"] = top_summary

    return lookup
