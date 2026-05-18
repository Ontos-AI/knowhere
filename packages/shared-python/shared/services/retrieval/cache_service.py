from __future__ import annotations

import hashlib
from typing import Any

from loguru import logger

from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.redis import RedisServiceFactory

_RETRIEVAL_CACHE_TTL_SECONDS = 300
_WORKFLOW_PLAN_CACHE_TTL_SECONDS = 600
_VERSION_FALLBACK = 0


def _namespace_version_key(*, user_id: str, namespace: str) -> str:
    namespace = normalize_retrieval_namespace(namespace)
    return f"retrieval:version:{user_id}:{namespace}"


def _normalize_exclude_sections(exclude_sections: list[dict[str, str]]) -> list[str]:
    normalized: list[str] = []
    for item in exclude_sections:
        if not isinstance(item, dict):
            continue
        document_id = str(item.get("document_id") or "").strip()
        section_path = str(item.get("section_path") or "").strip()
        if not document_id or not section_path:
            continue
        normalized.append(f"{document_id}:{section_path}")
    return sorted(set(normalized))


def _cache_shape_digest(
    *,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    data_type: int = 1,
    signal_paths: list[str] | None = None,
    filter_mode: str = "delete",
    channels: list[str] | None = None,
    channel_weights: dict[str, float] | None = None,
    rerank: bool = False,
    threshold: float = 0.0,
    internal_recall_k: int | None = None,
    decomposition_enabled: bool | None = None,
) -> str:
    normalized_excludes = sorted(exclude_document_ids)
    normalized_sections = _normalize_exclude_sections(exclude_sections)
    extra = "|".join(
        [
            str(data_type),
            ",".join(sorted(signal_paths or [])),
            filter_mode,
            ",".join(sorted(channels or [])),
            str(sorted((channel_weights or {}).items())),
            str(rerank),
            str(threshold),
            str(internal_recall_k),
            str(decomposition_enabled),
        ]
    )
    payload = f"{query}|{top_k}|{'|'.join(normalized_excludes)}|{'|'.join(normalized_sections)}|{extra}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _query_cache_key(
    *,
    user_id: str,
    namespace: str,
    version: int,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    **extra_params: Any,
) -> str:
    namespace = normalize_retrieval_namespace(namespace)
    digest = _cache_shape_digest(
        query=query,
        top_k=top_k,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
        **extra_params,
    )
    return f"retrieval:query:{user_id}:{namespace}:v{version}:{digest}"


async def get_retrieval_namespace_cache_version(*, user_id: str, namespace: str) -> int:
    redis_service = RedisServiceFactory.get_service()
    raw = await redis_service.get(
        _namespace_version_key(user_id=user_id, namespace=namespace),
        default=_VERSION_FALLBACK,
    )
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _VERSION_FALLBACK


async def bump_retrieval_namespace_cache_version(
    *, user_id: str, namespace: str
) -> int:
    redis_service = RedisServiceFactory.get_service()
    return await redis_service.incr(
        _namespace_version_key(user_id=user_id, namespace=namespace)
    )


async def invalidate_retrieval_cache_namespaces(
    *, user_id: str, namespaces: list[str]
) -> None:
    seen: set[str] = set()
    for raw_namespace in namespaces:
        namespace = normalize_retrieval_namespace(raw_namespace)
        if not namespace or namespace in seen:
            continue
        seen.add(namespace)
        try:
            await bump_retrieval_namespace_cache_version(
                user_id=user_id, namespace=namespace
            )
        except Exception as exc:
            logger.warning(
                f"Failed to invalidate retrieval cache namespace (ignored): user_id={user_id}, namespace={namespace}, error={exc}"
            )


async def get_cached_retrieval_query_result(
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    **extra_params: Any,
) -> tuple[int, dict[str, Any] | None]:
    version = await get_retrieval_namespace_cache_version(
        user_id=user_id, namespace=namespace
    )
    redis_service = RedisServiceFactory.get_service()
    cached = await redis_service.get(
        _query_cache_key(
            user_id=user_id,
            namespace=namespace,
            version=version,
            query=query,
            top_k=top_k,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            **extra_params,
        ),
        default=None,
    )
    return version, cached


async def set_cached_retrieval_query_result(
    *,
    user_id: str,
    namespace: str,
    version: int,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    response: dict[str, Any],
    **extra_params: Any,
) -> None:
    redis_service = RedisServiceFactory.get_service()
    await redis_service.set(
        _query_cache_key(
            user_id=user_id,
            namespace=namespace,
            version=version,
            query=query,
            top_k=top_k,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            **extra_params,
        ),
        response,
        ex=_RETRIEVAL_CACHE_TTL_SECONDS,
    )


def _workflow_plan_cache_key(*, user_id: str, namespace: str, query: str) -> str:
    namespace = normalize_retrieval_namespace(namespace)
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    return f"retrieval:workflow:plan:{user_id}:{namespace}:{digest}"


async def get_cached_workflow_plan(
    *,
    user_id: str,
    namespace: str,
    query: str,
) -> dict[str, Any] | None:
    redis_service = RedisServiceFactory.get_service()
    cached = await redis_service.get(
        _workflow_plan_cache_key(user_id=user_id, namespace=namespace, query=query),
        default=None,
    )
    return cached if isinstance(cached, dict) else None


async def set_cached_workflow_plan(
    *,
    user_id: str,
    namespace: str,
    query: str,
    plan: dict[str, Any],
) -> None:
    redis_service = RedisServiceFactory.get_service()
    await redis_service.set(
        _workflow_plan_cache_key(user_id=user_id, namespace=namespace, query=query),
        plan,
        ex=_WORKFLOW_PLAN_CACHE_TTL_SECONDS,
    )
