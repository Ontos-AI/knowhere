"""
summary_builder: Bottom-up recursive summarization for document navigation.

Reads doc_nav.json and in-memory chunks, generates ``summary`` at every
non-leaf via deterministic covers assembly or LLM aggregation (with self_only).
Persists document-level ``top_summary`` on doc_nav (default LLM); section-level
LLM remains opt-in via ``use_llm``.

Usage (standalone):
    from app.services.connect_builder.summary_builder import enrich_doc_nav_summaries
    enrich_doc_nav_summaries(document_workspace_dir, source_file="report.pdf", chunks=chunks)
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from openai.types.chat import ChatCompletionMessageParam


# ─── Constants ────────────────────────────────────────────────────────────────

# LLM trigger: sum of child contribution lengths + self_only must exceed this
SUMMARY_MAX_LEN = 100
# Deterministic (and top-level LLM output) head…tail token budget
DETERMINISTIC_SUMMARY_HEAD = 100
DETERMINISTIC_SUMMARY_TAIL = 100
# Navigation top-summary LLM max_tokens
NAVIGATION_TOP_SUMMARY_MAX_TOKENS = 200

SECTION_COVERS_PREFIX = "This section covers: "
DOCUMENT_INCLUDES_PREFIX = "This document includes: "


# ─── LLM Interface ───────────────────────────────────────────────────────────


def _build_scope_payload_text(
    *,
    node_name: str,
    self_only: str,
    child_rows: List[Tuple[str, str]],
) -> str:
    """Flatten SCOPE fields for language detection / logging."""
    titles = [title for title, _ in child_rows]
    covered = "\n".join(f"- [{title}] {contrib}" for title, contrib in child_rows)
    return "\n".join(
        [
            f"SCOPE_TITLE: {node_name}",
            f"self_only: {'yes' if self_only.strip() else 'no'}",
            f"children: {', '.join(titles)}",
            f"SELF_ONLY_CONTENT:\n{self_only.strip() or '(none)'}",
            f"COVERED_NODES:\n{covered or '(none)'}",
        ]
    )


def _llm_summarize(
    *,
    node_name: str,
    self_only: str,
    child_rows: List[Tuple[str, str]],
    max_tokens: int = 100,
) -> str:
    """Call LLM to produce a concise summary for one scope.

    Returns plain text summary, or "" on failure / null.
    """
    try:
        from shared.services.ai.prompt_service import build_prompt, _detect_text_language
        from shared.services.ai.llm_overrides import get_text_client

        payload_text = _build_scope_payload_text(
            node_name=node_name,
            self_only=self_only,
            child_rows=child_rows,
        )
        detected_lang = _detect_text_language(payload_text)
        child_titles = [title for title, _ in child_rows]
        covered_nodes = "\n".join(
            f"- [{title}] {contrib}" for title, contrib in child_rows
        )
        prompt, temperature, top_p, _prompt_max_tokens = build_prompt(
            task="file-summary",
            texts=payload_text,
            query="",
            paras={
                "max_tokens": max_tokens,
                "node_name": node_name,
                "lang": detected_lang,
                "has_self_only": bool(self_only.strip()),
                "child_titles": child_titles,
                "self_only_content": self_only.strip() or "(none)",
                "covered_nodes": covered_nodes or "(none)",
            },
        )
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": "you are a helpful assistant"},
            {"role": "user", "content": prompt},
        ]
        client, _ = get_text_client()
        resp = client.chat_completion(
            messages=messages,
            timeout=60,
            max_tokens=max_tokens,
            usage_task="finalization.doc_nav_summary",
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


# ─── doc_nav I/O ─────────────────────────────────────────────────────────────


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

    schema = ZipResultSchemaBuilder()
    doc_nav = schema.build_doc_nav(chunks, source_file_name)

    _save_doc_nav(file_dir, doc_nav)
    return nav_path


# ─── self_only + deterministic assembly ──────────────────────────────────────


def build_self_only_lookup(
    chunks: List[Dict[str, Any]],
    *,
    source_file_name: str = "",
) -> Dict[str, str]:
    """Map canonical section_path → concatenated exact-path chunk content.

    Exact path only (no descendants): same semantics as hydrate ``self_only``.
    """
    from shared.services.retrieval.search.lexical_text import section_path_from_chunk_path

    by_path: Dict[str, List[str]] = {}
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        raw_path = str(chunk.get("path") or "").strip()
        if not raw_path:
            continue
        section_path = section_path_from_chunk_path(
            raw_path,
            source_file_name=source_file_name,
        )
        if not section_path or section_path == "Root":
            continue
        content = str(chunk.get("content") or chunk.get("text") or "").strip()
        if not content:
            metadata = chunk.get("metadata") or {}
            if isinstance(metadata, dict):
                content = str(metadata.get("summary") or "").strip()
        if not content:
            continue
        by_path.setdefault(section_path, []).append(content)
    return {path: "\n".join(parts) for path, parts in by_path.items()}


def _node_section_path(node: Dict[str, Any], source_file_name: str) -> str:
    from shared.services.retrieval.search.lexical_text import section_path_from_chunk_path

    nav_path = str(node.get("path") or "").strip()
    if not nav_path:
        return ""
    return section_path_from_chunk_path(nav_path, source_file_name=source_file_name)


def _deterministic_section_summary(
    *,
    is_top_level: bool,
    self_only: str,
    child_titles: List[str],
) -> str:
    """covers/includes prefix → self_only → child titles; then head…tail whole string."""
    from shared.utils.text_utils import truncate_content_preview

    prefix = DOCUMENT_INCLUDES_PREFIX if is_top_level else SECTION_COVERS_PREFIX
    segments: List[str] = []
    self_text = (self_only or "").strip()
    if self_text:
        segments.append(self_text)
    if child_titles:
        segments.append(", ".join(child_titles))
    assembled = prefix + " ".join(segments) if segments else prefix.rstrip()
    return truncate_content_preview(
        assembled,
        head=DETERMINISTIC_SUMMARY_HEAD,
        tail=DETERMINISTIC_SUMMARY_TAIL,
    )


def _child_title_list(
    children: List[Dict[str, Any]],
    *,
    is_top_level: bool,
) -> List[str]:
    """All direct child titles (empty titles omitted); top-level skips 'root'."""
    titles: List[str] = []
    for child in children:
        title = str(child.get("title") or "").strip()
        if not title:
            continue
        if is_top_level and title.lower() == "root":
            continue
        titles.append(title)
    return titles


def _child_contribution_rows(
    children: List[Dict[str, Any]],
    *,
    is_top_level: bool,
) -> List[Tuple[str, str]]:
    """(title, contribution) where contribution = summary or title."""
    rows: List[Tuple[str, str]] = []
    for child in children:
        title = str(child.get("title") or "").strip()
        if is_top_level and title.lower() == "root":
            continue
        summary = str(child.get("summary") or "").strip()
        contrib = summary or title
        if not title and not contrib:
            continue
        rows.append((title or contrib, contrib))
    return rows


# ─── Recursive summarization on doc_nav sections ─────────────────────────────


def _recursive_summarize_nav(
    node: Dict[str, Any],
    use_llm: bool = True,
    is_top_level: bool = False,
    *,
    self_only_lookup: Optional[Dict[str, str]] = None,
    source_file_name: str = "",
) -> str:
    """Bottom-up recursive summarization on a doc_nav section node.

    - Leaf → keep existing summary.
    - Non-leaf → recurse children, then:
      - use_llm=False → always deterministic covers (titles ± self_only); ignore 100
      - use_llm=True → LLM only if sum(contrib lens)+len(self_only) > SUMMARY_MAX_LEN;
        otherwise same deterministic covers path
    """
    children = list(node.get("children") or [])
    title = str(node.get("title") or "")

    if not children:
        existing = str(node.get("summary") or "").strip()
        if existing:
            node["summary"] = existing
        return str(node.get("summary") or "")

    lookup = self_only_lookup or {}
    for child in children:
        _recursive_summarize_nav(
            child,
            use_llm=use_llm,
            is_top_level=False,
            self_only_lookup=lookup,
            source_file_name=source_file_name,
        )

    section_path = _node_section_path(node, source_file_name)
    self_only = ""
    if section_path and section_path != "Root":
        self_only = str(lookup.get(section_path) or "")
    if self_only.strip():
        node["self_summary"] = self_only.strip()
    elif "self_summary" in node:
        node.pop("self_summary", None)

    child_titles = _child_title_list(children, is_top_level=is_top_level)
    child_rows = _child_contribution_rows(children, is_top_level=is_top_level)
    contrib_len = sum(len(contrib) for _, contrib in child_rows) + len(self_only)

    deterministic = _deterministic_section_summary(
        is_top_level=is_top_level,
        self_only=self_only,
        child_titles=child_titles,
    )

    # use_llm=False → always title/covers path; 100-threshold only applies when LLM is on
    if not use_llm:
        result = deterministic
    elif contrib_len > SUMMARY_MAX_LEN:
        max_tokens = (
            NAVIGATION_TOP_SUMMARY_MAX_TOKENS if is_top_level else SUMMARY_MAX_LEN
        )
        result = _llm_summarize(
            node_name=title,
            self_only=self_only,
            child_rows=child_rows,
            max_tokens=max_tokens,
        )
    else:
        result = deterministic

    node["summary"] = result
    return result


def _doc_nav_has_enriched_summaries(doc_nav: Dict[str, Any]) -> bool:
    """True iff every non-leaf has a non-empty summary."""

    def _check_sections(sections: List[Dict[str, Any]]) -> bool:
        for section in sections:
            children = section.get("children", [])
            if not children:
                continue
            if not section.get("summary"):
                return False
            if not _check_sections(children):
                return False
        return True

    sections = doc_nav.get("sections", [])
    if not sections:
        return False
    has_non_leaf = any(s.get("children") for s in sections)
    if not has_non_leaf:
        return False
    return _check_sections(sections)


def _build_nav_top_summary(
    doc_nav: Dict[str, Any],
    use_llm: bool = True,
    *,
    self_only_lookup: Optional[Dict[str, str]] = None,
    source_file_name: str = "",
) -> str:
    """Build document-level top summary from already-enriched section nodes.

    Children are never re-summarized with LLM here — section LLM is controlled
    only by ``enrich_doc_nav_summaries(use_llm=...)``. This path may optionally
    LLM-aggregate the document overview from child contributions.
    """
    sections = list(doc_nav.get("sections") or [])
    if not sections:
        return ""

    file_name = source_file_name or str(doc_nav.get("file_name") or "")
    # Fill any missing child summaries deterministically without enabling LLM.
    for section in sections:
        if isinstance(section, dict):
            _recursive_summarize_nav(
                section,
                use_llm=False,
                self_only_lookup=self_only_lookup,
                source_file_name=file_name,
            )

    child_titles = _child_title_list(sections, is_top_level=True)
    child_rows = _child_contribution_rows(sections, is_top_level=True)
    contrib_len = sum(len(contrib) for _, contrib in child_rows)
    deterministic = _deterministic_section_summary(
        is_top_level=True,
        self_only="",
        child_titles=child_titles,
    )
    if not use_llm or contrib_len <= SUMMARY_MAX_LEN:
        return deterministic

    llm_result = _llm_summarize(
        node_name="Document Overview",
        self_only="",
        child_rows=child_rows,
        max_tokens=NAVIGATION_TOP_SUMMARY_MAX_TOKENS,
    )
    return llm_result or deterministic


def _persist_doc_nav_top_summary(
    *,
    file_dir: str,
    doc_nav: Dict[str, Any],
    top_summary: str,
) -> str:
    """Write document-level top_summary once onto doc_nav and save."""
    cleaned = str(top_summary or "").strip()
    if cleaned:
        doc_nav["top_summary"] = cleaned
    elif "top_summary" in doc_nav:
        doc_nav.pop("top_summary", None)
    _save_doc_nav(file_dir, doc_nav)
    return cleaned


def enrich_doc_nav_summaries(
    document_workspace_dir: str,
    source_file: Optional[str] = None,
    force: bool = False,
    use_llm: bool = False,
    *,
    top_summary_use_llm: bool = True,
    chunks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """Enrich doc_nav.json with bottom-up recursive summaries.

    Args:
        document_workspace_dir: Absolute path to the temporary document workspace.
        source_file: If given, only process this file. Otherwise process all.
        force: If True, regenerate even if summaries already exist.
        use_llm: Section-level LLM summaries (default off).
        top_summary_use_llm: Document-level top summary LLM (default on).
        chunks: In-memory parse chunks for exact-path self_only extraction.

    Returns:
        Dict mapping file_name → top-level summary string.
    """
    results: Dict[str, str] = {}
    section_mode = "LLM" if use_llm else "title-concat"
    top_mode = "LLM" if top_summary_use_llm else "title-concat"

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

        source_file_name = str(doc_nav.get("file_name") or file_name)
        self_only_lookup = build_self_only_lookup(
            list(chunks or []),
            source_file_name=source_file_name,
        )

        existing_top = str(doc_nav.get("top_summary") or "").strip()
        if (
            not force
            and _doc_nav_has_enriched_summaries(doc_nav)
            and existing_top
        ):
            logger.debug(
                f"Summaries already exist in {DOC_NAV_FILENAME} for {file_name}, skipping"
            )
            results[file_name] = existing_top
            continue

        if not force and _doc_nav_has_enriched_summaries(doc_nav):
            logger.info(
                f"📝 Building missing {DOC_NAV_FILENAME} top_summary for {file_name} "
                f"(top_mode={top_mode})"
            )
            top_summary = _build_nav_top_summary(
                doc_nav,
                use_llm=top_summary_use_llm,
                self_only_lookup=self_only_lookup,
                source_file_name=source_file_name,
            )
            results[file_name] = _persist_doc_nav_top_summary(
                file_dir=file_dir,
                doc_nav=doc_nav,
                top_summary=top_summary,
            )
            continue

        logger.info(
            f"📝 Enriching {DOC_NAV_FILENAME} summaries for {file_name} "
            f"(section_mode={section_mode}, top_mode={top_mode})"
        )

        for section in doc_nav.get("sections", []):
            _recursive_summarize_nav(
                section,
                use_llm=use_llm,
                self_only_lookup=self_only_lookup,
                source_file_name=source_file_name,
            )

        top_summary = _build_nav_top_summary(
            doc_nav,
            use_llm=top_summary_use_llm,
            self_only_lookup=self_only_lookup,
            source_file_name=source_file_name,
        )
        results[file_name] = _persist_doc_nav_top_summary(
            file_dir=file_dir,
            doc_nav=doc_nav,
            top_summary=top_summary,
        )
        logger.info(f"✅ doc_nav summaries saved for {file_name}")

    return results


def load_nav_top_summary(file_dir: str, file_name: str = "") -> str:
    """Load persisted doc_nav top_summary, with deterministic fallback."""
    doc_nav = _load_doc_nav(file_dir)
    if doc_nav is None:
        return ""
    existing = str(doc_nav.get("top_summary") or "").strip()
    if existing:
        return existing
    source_file_name = file_name or str(doc_nav.get("file_name") or "")
    return _build_nav_top_summary(
        doc_nav,
        use_llm=False,
        source_file_name=source_file_name,
    )


def build_section_summary_lookup(file_dir: str) -> Dict[str, str]:
    """Build a flat {section_path: summary} dict from all nodes in doc_nav.json.

    Keys use the DocumentSection.section_path format produced by
    ``section_path_from_chunk_path`` (strips the filename prefix,
    joins remaining parts with ``" / "``).
    """
    from shared.services.retrieval.search.lexical_text import section_path_from_chunk_path

    doc_nav = _load_doc_nav(file_dir)
    if doc_nav is None:
        return {}

    lookup: Dict[str, str] = {}
    source_file_name = str(doc_nav.get("file_name") or "")

    def _walk(node: Dict[str, Any]) -> None:
        nav_path = node.get("path", "")
        summary = node.get("summary", "")
        if nav_path and summary:
            section_path = section_path_from_chunk_path(
                nav_path,
                source_file_name=source_file_name,
            )
            if section_path and section_path != "Root":
                lookup[section_path] = summary
        for child in node.get("children", []):
            _walk(child)

    for section in doc_nav.get("sections", []):
        _walk(section)

    if "Root" not in lookup:
        top_summary = load_nav_top_summary(file_dir, source_file_name)
        if top_summary:
            lookup["Root"] = top_summary

    return lookup
