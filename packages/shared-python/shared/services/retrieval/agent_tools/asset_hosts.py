"""Asset -> hosting body section, shared by grep, recall and assets.

Image/table chunks are stored under their document's ``Root`` section; the
real placement is ``chunk_metadata.connect_to`` on the body chunk that embeds
them. Hosts are keyed by ``(document_id,
asset chunk_id)`` — chunk ids are content hashes and repeat across
documents — and read only from each document's current revision. When an
asset has several hosts they are kept in body order, so every tool that
picks one picks the same one.
"""

from __future__ import annotations

from shared.services.retrieval.corpus_revision_context import CorpusRevisionContext

from shared.services.retrieval.corpus_storage import CorpusStorage

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from shared.services.retrieval.agent_tools.registry import ToolContext
from shared.services.retrieval.agent_tools.scope import ScopeTarget
from shared.services.retrieval.hydration.row_utils import iter_connected_target_ids
from shared.services.retrieval.settings import ASSET_CHUNK_TYPES

_BODY_CHUNK_TYPES = ("text", "page")

AssetKey = tuple[str, str]


@dataclass(frozen=True)
class AssetHost:
    document_id: str
    section_path: str
    body_chunk_id: str
    source_file_name: str


def host_in_scope(scope: list[ScopeTarget], *, document_id: str, section_path: str) -> bool:
    """A host is in scope when its section lies in some target's subtree.

    An empty ``scope`` means no narrowing.
    """
    if not scope:
        return True
    for target in scope:
        if target.document_id != document_id:
            continue
        if target.section_path is None:
            return True
        if section_path == target.section_path or section_path.startswith(
            f"{target.section_path} / "
        ):
            return True
    return False


async def load_asset_hosts(
    ctx: ToolContext,
    *,
    document_ids: Iterable[str] | None,
    asset_ids: Iterable[str] | None = None,
) -> dict[AssetKey, list[AssetHost]]:
    """Every current-revision body chunk that connects to an asset.

    ``document_ids=None`` scans every document visible to the caller;
    ``asset_ids`` limits the result to those asset chunk ids.
    """
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(ctx.namespace)
    wanted = set(asset_ids) if asset_ids is not None else None
    stmt = (
        select(
            corpusStorage.DocumentChunk.document_id,
            corpusStorage.DocumentChunk.chunk_id,
            corpusStorage.DocumentChunk.chunk_metadata,
            corpusStorage.DocumentSection.section_path,
            corpusStorage.Document.source_file_name,
        )
        .select_from(corpusStorage.DocumentChunk)
        .join(corpusStorage.Document, corpusStorage.Document.document_id == corpusStorage.DocumentChunk.document_id)
        .outerjoin(corpusStorage.DocumentSection, corpusStorage.DocumentSection.section_id == corpusStorage.DocumentChunk.section_id)
        .where(
            corpusStorage.Document.user_id == corpusStorage.resolve_owner(ctx.user_id),
            corpusStorage.Document.namespace == ctx.namespace,
            corpusStorage.Document.status == "active",
            CorpusRevisionContext.build_revision_column(corpusStorage.Document) == corpusStorage.DocumentChunk.job_result_id,
            ctx.document_scope.predicate(corpusStorage.Document.document_id),
            corpusStorage.DocumentChunk.chunk_type.in_(_BODY_CHUNK_TYPES),
        )
        .order_by(corpusStorage.DocumentChunk.document_id, corpusStorage.DocumentChunk.sort_order, corpusStorage.DocumentChunk.chunk_id)
    )
    if document_ids is not None:
        stmt = stmt.where(corpusStorage.Document.document_id.in_(sorted(set(document_ids))))

    hosts: dict[AssetKey, list[AssetHost]] = {}
    for document_id, chunk_id, metadata, section_path, source_file_name in (
        await ctx.db.execute(stmt)
    ).all():
        for target_id in iter_connected_target_ids({"chunk_metadata": metadata}):
            if wanted is not None and target_id not in wanted:
                continue
            hosts.setdefault((str(document_id), target_id), []).append(
                AssetHost(
                    document_id=str(document_id),
                    section_path=str(section_path or ""),
                    body_chunk_id=str(chunk_id),
                    source_file_name=str(source_file_name or ""),
                )
            )
    return hosts


def in_scope_hosts(
    hosts: dict[AssetKey, list[AssetHost]],
    key: AssetKey,
    scope: list[ScopeTarget],
) -> list[AssetHost]:
    return [
        host
        for host in hosts.get(key, [])
        if host_in_scope(scope, document_id=host.document_id, section_path=host.section_path)
    ]


def hosted_section_path(
    hosts: dict[AssetKey, list[AssetHost]],
    key: AssetKey,
    scope: list[ScopeTarget],
    *,
    stored_path: Any,
) -> tuple[str, bool]:
    """``(section_path, hosted)`` for an asset row: its first in-scope host,
    or its own stored (Root) path with ``hosted=False``."""
    candidates = in_scope_hosts(hosts, key, scope)
    if candidates:
        return candidates[0].section_path, True
    return str(stored_path or "Root"), False


async def host_paths_for_hits(
    ctx: ToolContext,
    hits: list[dict[str, Any]],
    scope: list[ScopeTarget],
) -> dict[AssetKey, tuple[str, bool]]:
    """``(section_path, hosted)`` for every image/table hit, same rule as assets."""
    asset_hits = [
        hit
        for hit in hits
        if str(hit.get("chunk_type") or "").strip() in ASSET_CHUNK_TYPES
    ]
    if not asset_hits:
        return {}
    hosts = await load_asset_hosts(
        ctx,
        document_ids={str(hit.get("document_id") or "") for hit in asset_hits},
        asset_ids={str(hit.get("chunk_id") or "") for hit in asset_hits},
    )
    resolved: dict[AssetKey, tuple[str, bool]] = {}
    for hit in asset_hits:
        key = (str(hit.get("document_id") or ""), str(hit.get("chunk_id") or ""))
        resolved[key] = hosted_section_path(
            hosts, key, scope, stored_path=hit.get("section_path")
        )
    return resolved
