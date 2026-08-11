"""Asset SEARCH channel aligned with Knowhere ``assets.py``.

Pipeline (same shape as production ``search_assets_step``):

1. Document-bound scope gather by ``chunk_type`` (``asset_filter_step``).
2. Asset inspector: tables → text LLM; images → VLM when available, else
   text LLM fallback (and VLM failure also falls back).
3. Matched assets only are added to evidence; inspector context is injected
   into the next harvest observation (``NavState.asset_observation_context``).

Namespace / unbound scope is skipped — never scan the whole corpus.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .nav_address import NavLevel, address_level, owner_document
from .nav_knowhere import is_root_section

# Knowhere DocumentChunk.chunk_type values + harvest JSON kind aliases.
_KIND_TO_CHUNK_TYPE = {
    "image": "image",
    "images": "image",
    "table": "table",
    "tables": "table",
}

# Soft prompt char budget for the text inspector (episode budget is separate).
_ASSET_FILTER_PROMPT_CHAR_BUDGET = 24000

NavVlmBackend = Callable[..., Any]
_vlm_backend: Optional[NavVlmBackend] = None


def set_nav_vlm_backend(fn: Optional[NavVlmBackend]) -> None:
    """Install or clear an injected VLM backend (tests / Knowhere vlm_fn)."""
    global _vlm_backend
    _vlm_backend = fn


def get_nav_vlm_backend() -> Optional[NavVlmBackend]:
    return _vlm_backend


def asset_chunk_type(kind: str) -> Optional[str]:
    """Map harvest ``kind`` to Knowhere ``chunk_type``, or None if unknown."""
    return _KIND_TO_CHUNK_TYPE.get(str(kind or "").strip().lower())


def parse_search_assets(raw: Any) -> List[Dict[str, str]]:
    """Normalize harvest ``search_assets`` entries to ``{kind,query,scope,asset_type}``."""
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        asset_type = asset_chunk_type(kind)
        if not asset_type:
            continue
        query = str(item.get("query") or "").strip()
        scope = str(item.get("scope") or "").strip()
        out.append(
            {
                "kind": kind,
                "query": query,
                "scope": scope,
                "asset_type": asset_type,
            }
        )
    return out


def _provider(ts: Any) -> Any:
    return getattr(ts, "_provider", None)


def resolve_asset_search_scope(
    ts: Any,
    *,
    requested_scope: Optional[str],
    default_scope: Optional[str],
    fallback_doc_id: str = "",
) -> Tuple[str, str, Optional[str]]:
    """Bind asset SEARCH to one document.

    Returns ``(effective_scope, doc_id, skip_reason)``.
    """
    raw = str(requested_scope or "").strip() or str(default_scope or "").strip()
    if not raw:
        return "", "", "skipped_no_document_scope"

    level = address_level(ts, raw)
    if level == NavLevel.NAMESPACE:
        return "", "", "skipped_namespace_scope"

    doc = owner_document(ts, raw, fallback_doc_id)
    if not doc and level == NavLevel.DOCUMENT:
        doc = raw
    doc = str(doc or "").strip()
    if not doc:
        return "", "", "skipped_no_document_scope"

    if level in (NavLevel.SECTION, NavLevel.CHUNK):
        owner = owner_document(ts, raw, doc)
        if owner and owner != doc:
            return "", "", "skipped_cross_document_scope"

    return raw, doc, None


def _section_ids_in_document(ts: Any, doc_id: str) -> List[str]:
    doc = str(doc_id or "").strip()
    if not doc:
        return []

    relations = getattr(ts, "section_relation_ids", None)
    if callable(relations):
        _anc, descendants = relations(doc, doc)
        got = [str(x) for x in (descendants or ()) if str(x).strip()]
        if got:
            return list(dict.fromkeys(got))

    provider = _provider(ts)
    if provider is not None and callable(getattr(provider, "all_section_ids", None)):
        out = [
            sid
            for sid in (str(x) for x in provider.all_section_ids() if str(x).strip())
            if owner_document(ts, sid, doc) == doc
        ]
        if out:
            return list(dict.fromkeys(out))

    roots_fn = getattr(ts, "sections_for_doc", None)
    if callable(roots_fn):
        out: List[str] = []
        for root in (str(x) for x in (roots_fn(doc) or ()) if str(x).strip()):
            if owner_document(ts, root, doc) != doc:
                continue
            out.extend(_section_ids_under_bound_scope(ts, root, doc))
        return list(dict.fromkeys(out))
    return []


def _section_ids_under_bound_scope(ts: Any, scope: str, doc_id: str) -> List[str]:
    sid = str(scope or "").strip()
    doc = str(doc_id or "").strip()
    if not sid or not doc:
        return []

    level = address_level(ts, sid)
    if level == NavLevel.DOCUMENT or sid == doc:
        return _section_ids_in_document(ts, doc)

    provider = _provider(ts)
    out: List[str] = [sid]
    relations = getattr(ts, "section_relation_ids", None)
    if callable(relations):
        _anc, descendants = relations(sid, doc)
        out.extend(str(x) for x in (descendants or ()) if str(x).strip())
    elif provider is not None and callable(getattr(provider, "relations", None)):
        _anc, descendants = provider.relations(sid)
        out.extend(str(x) for x in (descendants or ()) if str(x).strip())

    return [
        s for s in dict.fromkeys(out) if owner_document(ts, s, doc) == doc or s == sid
    ]


def gather_scoped_asset_candidates(
    ts: Any,
    *,
    asset_type: str,
    scope: Optional[str],
    doc_id: str = "",
) -> List[Dict[str, Any]]:
    """Units under a document-bound scope with matching ``chunk_type``."""
    from ._compat import Chunk  # type: ignore

    wanted = str(asset_type or "").strip().lower()
    if wanted not in {"image", "table"}:
        return []

    resolved_doc = str(doc_id or "").strip()
    sid = str(scope or "").strip()
    if not sid or not resolved_doc:
        return []

    provider = _provider(ts)
    self_units = getattr(provider, "self_units", None) if provider is not None else None
    unit_text = getattr(provider, "unit_text", None) if provider is not None else None
    if not callable(self_units) or not callable(unit_text):
        return []

    section_ids = _section_ids_under_bound_scope(ts, sid, resolved_doc)
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    section_path_fn = getattr(provider, "section_path", None)
    for section_id in section_ids:
        owner = owner_document(ts, section_id, resolved_doc)
        if owner and owner != resolved_doc:
            continue
        # Align with Knowhere: never treat Root as an asset owner.
        if is_root_section(provider, section_id):
            continue
        owner_path = (
            str(section_path_fn(section_id) or "").strip()
            if callable(section_path_fn)
            else str(section_id)
        )
        for unit in self_units(section_id) or ():
            if str(getattr(unit, "chunk_type", "") or "").strip().lower() != wanted:
                continue
            chunk_id = str(getattr(unit, "chunk_id", "") or "").strip()
            if not chunk_id or chunk_id in seen:
                continue
            text = str(unit_text(unit) or "").strip()
            if not text:
                continue
            seen.add(chunk_id)
            meta = dict(getattr(unit, "metadata", None) or {})
            file_path = str(
                getattr(unit, "file_path", "")
                or meta.get("file_path")
                or getattr(unit, "source_chunk_path", "")
                or getattr(unit, "content", "")
                or ""
            ).strip()
            summary = str(meta.get("summary") or "").strip()
            chunk = Chunk(
                node_id=chunk_id,
                doc_id=owner or resolved_doc,
                text=text,
                line_ids=(int(getattr(unit, "sort_order", 0) or 0),),
                section_id=section_id,
            )
            out.append(
                {
                    "chunk_id": chunk_id,
                    "chunk_type": wanted,
                    "content": str(getattr(unit, "content", "") or ""),
                    "file_path": file_path,
                    "section_id": section_id,
                    "section_path": owner_path or str(section_id),
                    "owner_section_path": owner_path or str(section_id),
                    "summary": summary,
                    "chunk_metadata": meta,
                    "display_text": text,
                    "chunk": chunk,
                    "url": str(meta.get("url") or meta.get("presigned_url") or "").strip(),
                }
            )
    out.sort(key=lambda a: (min(a["chunk"].line_ids or (0,)), a["chunk_id"]))
    return out


def gather_scoped_asset_chunks(
    ts: Any,
    *,
    asset_type: str,
    scope: Optional[str],
    doc_id: str = "",
) -> List[Any]:
    """Back-compat: Chunk list for scope gather (no inspector)."""
    return [
        c["chunk"]
        for c in gather_scoped_asset_candidates(
            ts, asset_type=asset_type, scope=scope, doc_id=doc_id
        )
    ]



def _markdown_cell(value: str) -> str:
    return (
        str(value or "")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("|", "\\|")
        .strip()
    )


def _format_asset_candidates_table(candidates: List[Dict[str, str]]) -> str:
    lines = [
        "| ID | File | Summary / content signal |",
        "|---|---|---|",
    ]
    for candidate in candidates:
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(candidate.get("id", "")),
                    _markdown_cell(candidate.get("file", "")),
                    _markdown_cell(candidate.get("desc", "")),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _format_asset_filter_prompt(
    query: str,
    asset_type: str,
    candidates: List[Dict[str, str]],
) -> str:
    type_label = "images" if asset_type == "image" else "tables"
    items_text = _format_asset_candidates_table(candidates)
    example_id = candidates[0]["id"] if candidates else ("I1" if asset_type == "image" else "T1")
    return (
        f"You are an asset relevance filter.\n\n"
        f"Original user query: {query}\n\n"
        f"Below are {len(candidates)} {type_label} from a document. "
        f"Select ONLY assets that directly satisfy the user's query.\n\n"
        f"Selection policy:\n"
        f"- Match the requested asset type and the requested subject. "
        f"Being an image/chart/table is not enough.\n"
        f"- Do not select assets only because they belong to the same broad "
        f"domain as the query.\n"
        f"- Do not broaden specific market, instrument, company, metric, or "
        f"entity terms. Neighboring topics are not matches unless the candidate "
        f"explicitly connects them to the requested subject.\n"
        f"- Treat words like \"all\" as all relevant assets, not all visible "
        f"candidates.\n"
        f"- If the file name, summary, or content signal does not "
        f"directly support relevance, leave it out.\n"
        f"- If uncertain, do not select the asset.\n\n"
        f"=== Candidate {type_label.title()} ===\n{items_text}\n=== End ===\n\n"
        f"Return ONLY a JSON array of matching row IDs, e.g.: "
        f'["{example_id}"]\n'
        f"If none are relevant, return an empty array: []\n"
        f"Do not include any explanation."
    )


def _fit_text_to_char_budget(text: str, char_budget: int) -> str:
    text = text.strip()
    if not text or len(text) <= char_budget:
        return text
    return text[: max(0, char_budget)].rstrip() + "…"


def _project_assets_for_text_filter(
    *,
    asset_type: str,
    assets: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, str]], Set[str], Dict[str, str]]:
    projected: List[Dict[str, str]] = []
    valid_ids: Set[str] = set()
    id_to_chunk_id: Dict[str, str] = {}
    for index, asset in enumerate(assets, start=1):
        chunk_id = str(asset.get("chunk_id") or "")
        if not chunk_id:
            continue
        row_id = f"I{index}" if asset_type == "image" else f"T{index}"
        summary = str(asset.get("summary") or "").strip()
        file_path = str(asset.get("file_path") or "")
        content = str(asset.get("content") or "").strip()
        display = str(asset.get("display_text") or "").strip()
        description = summary or display or (content if asset_type == "table" else "")
        projected.append({"id": row_id, "file": file_path, "desc": description})
        valid_ids.add(row_id)
        id_to_chunk_id[row_id] = chunk_id

    if not projected:
        return projected, valid_ids, id_to_chunk_id

    prompt = _format_asset_filter_prompt("q", asset_type, projected)
    if len(prompt) <= _ASSET_FILTER_PROMPT_CHAR_BUDGET:
        return projected, valid_ids, id_to_chunk_id

    # Compact descriptions so the full prompt fits the soft envelope.
    overhead = len(_format_asset_filter_prompt("q", asset_type, [
        {"id": item["id"], "file": item["file"], "desc": ""} for item in projected
    ]))
    desc_budget = max(_ASSET_FILTER_PROMPT_CHAR_BUDGET - overhead, len(projected))
    per_item = max(desc_budget // len(projected), 8)
    compacted = [
        {
            "id": item["id"],
            "file": item["file"],
            "desc": _fit_text_to_char_budget(item["desc"], per_item),
        }
        for item in projected
    ]
    return compacted, valid_ids, id_to_chunk_id


def _parse_asset_filter_response(text: str, valid_ids: Set[str]) -> List[str]:
    text = (text or "").strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return [str(item) for item in result if str(item) in valid_ids]
    except (ValueError, json.JSONDecodeError):
        pass

    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        try:
            result = json.loads(fence_match.group(1).strip())
            if isinstance(result, list):
                return [str(item) for item in result if str(item) in valid_ids]
        except (ValueError, json.JSONDecodeError):
            pass

    bracket_match = re.search(r"\[.*?\]", text, re.DOTALL)
    if bracket_match:
        try:
            result = json.loads(bracket_match.group())
            if isinstance(result, list):
                return [str(item) for item in result if str(item) in valid_ids]
        except (ValueError, json.JSONDecodeError):
            pass
    return []


def _search_assets_via_text_llm(
    *,
    query: str,
    asset_type: str,
    assets: Sequence[Dict[str, Any]],
    config: Any,
) -> List[str]:
    from .nav_llm import nav_chat, resolve_nav_model

    projected, valid_ids, id_to_chunk_id = _project_assets_for_text_filter(
        asset_type=asset_type, assets=assets
    )
    if not projected:
        return []

    prompt = _format_asset_filter_prompt(query, asset_type, projected)
    model = resolve_nav_model(
        model=str(getattr(config, "llm_model", "") or ""),
        model_env="NAV_LLM_MODEL",
        fallback_envs=(),
    )
    try:
        cached = nav_chat(
            purpose="nav_asset_filter_v1",
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=max(256, int(getattr(config, "llm_max_tokens", 256) or 256)),
            context="Nav Asset Filter",
            usage_tag="nav_asset_filter",
        )
        text = str(cached.get("content") or "")
        # Some models wrap the array in {"ids":[...]}; accept either.
        selected = _parse_asset_filter_response(text, valid_ids)
        if not selected:
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    for key in ("ids", "selected", "matches", "result"):
                        if isinstance(obj.get(key), list):
                            selected = [
                                str(x) for x in obj[key] if str(x) in valid_ids
                            ]
                            break
            except (ValueError, json.JSONDecodeError):
                pass
        return [
            id_to_chunk_id[row_id]
            for row_id in selected
            if row_id in id_to_chunk_id
        ]
    except Exception:
        return []


def _resolve_image_url(asset: Dict[str, Any]) -> str:
    url = str(asset.get("url") or "").strip()
    if url:
        return url
    meta = asset.get("chunk_metadata") or {}
    if isinstance(meta, dict):
        for key in ("url", "presigned_url", "image_url"):
            got = str(meta.get(key) or "").strip()
            if got:
                return got
    return ""


def _search_images_via_vlm(
    *,
    query: str,
    assets: Sequence[Dict[str, Any]],
    vlm_fn: NavVlmBackend,
) -> Tuple[List[str], Optional[str]]:
    """VLM image filter when URLs exist; otherwise signal fallback."""
    candidates: List[Tuple[str, str, str]] = []
    valid_ids: Set[str] = set()
    id_to_chunk_id: Dict[str, str] = {}
    for index, asset in enumerate(assets, start=1):
        chunk_id = str(asset.get("chunk_id") or "")
        url = _resolve_image_url(asset)
        if not chunk_id or not url:
            continue
        row_id = f"I{index}"
        candidates.append((row_id, str(asset.get("file_path") or ""), url))
        valid_ids.add(row_id)
        id_to_chunk_id[row_id] = chunk_id

    if not candidates:
        return [], "no_image_urls"

    content_parts: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"You are an image relevance filter.\n\n"
                f"Original user query: {query}\n\n"
                f"Below are {len(candidates)} images from a document. "
                f"Look at each image and select ONLY images that directly "
                f"satisfy the user's query.\n\n"
                f"Return ONLY a JSON array of matching row IDs. "
                f"If none are relevant, return [].\n"
            ),
        }
    ]
    for row_id, file_path, url in candidates:
        content_parts.append({"type": "text", "text": f"\n{row_id} | {file_path}\n"})
        content_parts.append({"type": "image_url", "image_url": {"url": url}})

    try:
        response = vlm_fn(
            purpose="nav_asset_vlm_v1",
            messages=[{"role": "user", "content": content_parts}],
        )
        if isinstance(response, dict):
            text = str(response.get("content") or "")
        else:
            text = str(response or "")
        selected = _parse_asset_filter_response(text, valid_ids)
        return [
            id_to_chunk_id[row_id]
            for row_id in selected
            if row_id in id_to_chunk_id
        ], None
    except Exception as exc:
        return [], f"vlm_failed:{type(exc).__name__}"


def search_assets_step(
    *,
    query: str,
    asset_type: str,
    candidates: Sequence[Dict[str, Any]],
    config: Any,
) -> Dict[str, Any]:
    """Knowhere-aligned inspector over already-gathered candidates."""
    assets = list(candidates)
    if not assets:
        return {
            "status": "empty",
            "status_detail": "",
            "matched_assets": [],
            "candidate_count": 0,
        }

    asset_by_id = {
        str(a.get("chunk_id") or ""): a
        for a in assets
        if str(a.get("chunk_id") or "")
    }
    status_detail = ""
    selected_ids: List[str] = []

    if asset_type == "image":
        vlm_fn = get_nav_vlm_backend()
        if vlm_fn is None:
            selected_ids = _search_assets_via_text_llm(
                query=query, asset_type=asset_type, assets=assets, config=config
            )
            status = "fallback_matched" if selected_ids else "fallback_empty"
            status_detail = "vlm_unavailable_text_fallback"
        else:
            selected_ids, vlm_error = _search_images_via_vlm(
                query=query, assets=assets, vlm_fn=vlm_fn
            )
            if vlm_error:
                selected_ids = _search_assets_via_text_llm(
                    query=query, asset_type=asset_type, assets=assets, config=config
                )
                status = "fallback_matched" if selected_ids else "fallback_empty"
                status_detail = f"vlm_failed_text_fallback:{vlm_error}"
            else:
                status = "matched" if selected_ids else "empty"
    else:
        selected_ids = _search_assets_via_text_llm(
            query=query, asset_type=asset_type, assets=assets, config=config
        )
        status = "matched" if selected_ids else "empty"

    matched = [asset_by_id[cid] for cid in selected_ids if cid in asset_by_id]
    return {
        "status": status,
        "status_detail": status_detail,
        "matched_assets": matched,
        "candidate_count": len(asset_by_id),
    }


def format_asset_observation_context(
    *,
    kind: str,
    asset_type: str,
    search_query: str,
    matched_assets: Sequence[Dict[str, Any]],
    status: str = "empty",
    status_detail: str = "",
) -> str:
    """Next-step observation block (Knowhere ``_format_asset_context``)."""
    tool_name = "SEARCH_IMAGES" if asset_type == "image" else "SEARCH_TABLES"
    kind_norm = str(kind or "").strip().lower()
    if kind_norm in {"search_images", "images", "image"}:
        tool_name = "SEARCH_IMAGES"
    elif kind_norm in {"search_tables", "tables", "table"}:
        tool_name = "SEARCH_TABLES"
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
    for i, asset in enumerate(matched_assets, 1):
        file_path = str(asset.get("file_path") or asset.get("chunk_id") or "")
        lines.append(f"  {i}. {file_path}")
    owners = []
    seen: Set[str] = set()
    for asset in matched_assets:
        owner = str(
            asset.get("owner_section_path") or asset.get("section_path") or ""
        ).strip()
        if owner and owner not in seen:
            seen.add(owner)
            owners.append(owner)
    if owners:
        lines.append("Owner sections with matching assets:")
        for owner in owners:
            lines.append(f'  - "{owner}"')
    lines.append(
        "Use these asset results and owner sections to decide collect, "
        "finish, or further navigation."
    )
    lines.append(f"=== End {tool_name} Results ===")
    return "\n".join(lines)


def apply_search_assets(
    ts: Any,
    state: Any,
    config: Any,
    *,
    requests: Sequence[Dict[str, str]],
    default_scope: Optional[str],
) -> Tuple[int, List[Dict[str, Any]]]:
    """Inspect scoped assets; add matches to evidence; inject observation context."""
    from .nav_agent import _add_scored

    bonus = float(getattr(config, "read_score_bonus", 0.0) or 0.0)
    trace: List[Dict[str, Any]] = []
    total = 0
    fallback_doc = str(getattr(state, "doc_id", "") or "")
    context_blocks: List[str] = []

    for req in requests:
        asset_type = str(req.get("asset_type") or "").strip()
        query = str(req.get("query") or "").strip() or str(
            getattr(state, "query", "") or ""
        )
        requested = str(req.get("scope") or "").strip()
        scope, doc, skip_reason = resolve_asset_search_scope(
            ts,
            requested_scope=requested,
            default_scope=default_scope,
            fallback_doc_id=fallback_doc,
        )
        if skip_reason:
            trace.append(
                {
                    "kind": req.get("kind", ""),
                    "asset_type": asset_type,
                    "query": query,
                    "scope": requested or (default_scope or ""),
                    "doc_id": "",
                    "n_candidates": 0,
                    "n_matched": 0,
                    "n_added": 0,
                    "status": "skipped",
                    "skip_reason": skip_reason,
                }
            )
            continue

        candidates = gather_scoped_asset_candidates(
            ts, asset_type=asset_type, scope=scope, doc_id=doc
        )
        result = search_assets_step(
            query=query,
            asset_type=asset_type,
            candidates=candidates,
            config=config,
        )
        matched = list(result.get("matched_assets") or [])
        scored = [(a["chunk"], bonus) for a in matched if a.get("chunk") is not None]
        added = _add_scored(state, scored) if scored else 0
        total += added

        block = format_asset_observation_context(
            kind=str(req.get("kind") or ""),
            asset_type=asset_type,
            search_query=query,
            matched_assets=matched,
            status=str(result.get("status") or "empty"),
            status_detail=str(result.get("status_detail") or ""),
        )
        context_blocks.append(block)

        trace.append(
            {
                "kind": req.get("kind", ""),
                "asset_type": asset_type,
                "query": query,
                "scope": scope,
                "doc_id": doc,
                "n_candidates": int(result.get("candidate_count") or len(candidates)),
                "n_matched": len(matched),
                "n_added": added,
                "status": result.get("status", ""),
                "status_detail": result.get("status_detail", ""),
                "matched_chunk_ids": [str(a.get("chunk_id") or "") for a in matched],
            }
        )

    if context_blocks:
        prev = str(getattr(state, "asset_observation_context", "") or "").strip()
        joined = "\n\n".join(context_blocks)
        state.asset_observation_context = (
            f"{prev}\n\n{joined}".strip() if prev else joined
        )

    return total, trace
