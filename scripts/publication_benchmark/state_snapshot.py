"""Normalized publication state snapshots.

Snapshots keep counts, digests, and opaque references only. Raw user ids,
namespaces, source file names, section paths, chunk text, tokens, and artifact
paths never leave the database.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

STATE_SNAPSHOT_SCHEMA_VERSION: str = "publication-state-snapshot/2"
SEMANTIC_STATE_SCHEMA_VERSION: str = "publication-semantic-state/1"
_MAX_DOCUMENT_ROWS: int = 50


class StateSnapshotError(ValueError):
    """Raised when a publication state cannot be compared safely."""


def opaque_reference(value: object) -> str:
    """Return an opaque reference for one database identifier."""
    raw = str(value or "")
    return "ref-" + sha256(raw.encode("utf-8")).hexdigest()[:16]


def opaque_scope_fingerprint(user_id: str, namespace: str) -> str:
    """Return the opaque scope fingerprint used in reports."""
    raw = f"{user_id}\0{namespace}"
    return "scope-" + sha256(raw.encode("utf-8")).hexdigest()[:16]


def capture_state_snapshot(
    db: Session,
    *,
    user_id: str,
    namespace: str,
) -> dict[str, Any]:
    """Capture the normalized publication state of one scope."""
    parameters = {"user_id": user_id, "namespace": namespace}
    scope_filter = "user_id = :user_id AND namespace = :namespace"
    snapshot: dict[str, Any] = {
        "schema_version": STATE_SNAPSHOT_SCHEMA_VERSION,
        "scope_ref": opaque_scope_fingerprint(user_id, namespace),
        "relation_counts": {},
        "chunk_type_counts": {},
        "namespace_generation": None,
        "namespace_snapshot": None,
        "serving_revision_manifests": [],
        "documents": [],
        "source_file_name_refs": [],
        "semantic_fingerprints": {},
    }

    counts: dict[str, int] = {
        "documents_active": _scalar(
            db,
            f"SELECT count(*) FROM documents WHERE {scope_filter} "
            "AND status = 'active'",
            parameters,
        ),
        "documents_archived": _scalar(
            db,
            f"SELECT count(*) FROM documents WHERE {scope_filter} "
            "AND status <> 'active'",
            parameters,
        ),
        "document_sections": _scalar(
            db,
            f"SELECT count(*) FROM document_sections WHERE {scope_filter}",
            parameters,
        ),
        "document_chunks": _scalar(
            db,
            f"SELECT count(*) FROM document_chunks WHERE {scope_filter}",
            parameters,
        ),
        "document_map_units": _scalar(
            db,
            f"SELECT count(*) FROM document_map_units WHERE document_id IN "
            f"(SELECT document_id FROM documents WHERE {scope_filter})",
            parameters,
        ),
        "document_map_unit_tokens": _scalar(
            db,
            "SELECT count(*) FROM document_map_unit_tokens WHERE map_unit_id IN "
            "(SELECT id FROM document_map_units WHERE document_id IN "
            f"(SELECT document_id FROM documents WHERE {scope_filter}))",
            parameters,
        ),
        "document_map_unit_indexes": _scalar(
            db,
            "SELECT count(*) FROM document_map_unit_indexes WHERE document_id IN "
            f"(SELECT document_id FROM documents WHERE {scope_filter})",
            parameters,
        ),
        "graph_nodes": _scalar(
            db,
            f"SELECT count(*) FROM graph_nodes WHERE {scope_filter}",
            parameters,
        ),
        "graph_edges": _scalar(
            db,
            "SELECT count(*) FROM graph_edges WHERE source_node_id IN "
            f"(SELECT node_id FROM graph_nodes WHERE {scope_filter})",
            parameters,
        ),
    }
    snapshot["relation_counts"] = counts
    snapshot["chunk_type_counts"] = {
        str(row["chunk_type"]): int(row["total"])
        for row in _rows(
            db,
            f"SELECT chunk_type, count(*) AS total FROM document_chunks "
            f"WHERE {scope_filter} GROUP BY chunk_type ORDER BY chunk_type",
            parameters,
        )
    }
    snapshot["namespace_generation"] = _optional_scalar(
        db,
        f"SELECT generation FROM retrieval_namespace_generations WHERE {scope_filter}",
        parameters,
    )
    snapshot["namespace_snapshot"] = _namespace_snapshot_row(db, parameters)
    snapshot["serving_revision_manifests"] = [
        {
            "document_ref": opaque_reference(row["document_id"]),
            "revision_ref": opaque_reference(row["job_result_id"]),
            "format_version": int(row["format_version"]),
            "checksum": str(row["checksum"]),
            "payload_bytes": int(row["payload_bytes"]),
        }
        for row in _rows(
            db,
            "SELECT document_id, job_result_id, format_version, checksum, "
            "length(payload_zlib) AS payload_bytes FROM "
            "retrieval_serving_revision_manifests WHERE document_id IN "
            f"(SELECT document_id FROM documents WHERE {scope_filter}) "
            "ORDER BY document_id, job_result_id LIMIT :row_limit",
            {**parameters, "row_limit": _MAX_DOCUMENT_ROWS},
        )
    ]
    snapshot["documents"] = [
        {
            "document_ref": opaque_reference(row["document_id"]),
            "revision_ref": opaque_reference(row["current_job_result_id"]),
            "status": str(row["status"]),
            "section_count": int(row["section_count"]),
            "chunk_count": int(row["chunk_count"]),
            "map_unit_count": int(row["map_unit_count"]),
        }
        for row in _rows(
            db,
            "SELECT d.document_id, d.current_job_result_id, d.status, "
            "(SELECT count(*) FROM document_sections s WHERE "
            "s.document_id = d.document_id) AS section_count, "
            "(SELECT count(*) FROM document_chunks c WHERE "
            "c.document_id = d.document_id) AS chunk_count, "
            "(SELECT count(*) FROM document_map_units u WHERE "
            "u.document_id = d.document_id) AS map_unit_count "
            f"FROM documents d WHERE d.{scope_filter} "
            "ORDER BY d.document_id LIMIT :row_limit",
            {**parameters, "row_limit": _MAX_DOCUMENT_ROWS},
        )
    ]
    source_name_rows = _rows(
        db,
        "SELECT source_file_name FROM documents WHERE user_id = :user_id "
        "AND namespace = :namespace ORDER BY source_file_name",
        parameters,
    )
    snapshot["source_file_name_refs"] = [
        opaque_reference(row["source_file_name"]) for row in source_name_rows
    ]
    snapshot["semantic_fingerprints"] = capture_semantic_fingerprints(
        db,
        user_id=user_id,
        namespace=namespace,
    )
    return snapshot


def canonical_semantic_digest(value: object) -> str:
    """Hash a canonical semantic projection without persisting source values."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def normalize_serving_payload(
    payload: Mapping[str, Any],
    *,
    document_names: Mapping[str, str],
) -> dict[str, Any]:
    """Replace generated document and section IDs with semantic identities."""
    raw_sections = payload.get("sections") or []
    if not isinstance(raw_sections, list):
        raise StateSnapshotError("serving sections must be a list")
    section_paths = {
        str(section["section_id"]): str(section["section_path"])
        for section in raw_sections
        if isinstance(section, Mapping)
        and section.get("section_id")
        and section.get("section_path")
    }
    if len(section_paths) != len(raw_sections):
        raise StateSnapshotError("serving sections require unique IDs and paths")
    if len(set(section_paths.values())) != len(section_paths):
        raise StateSnapshotError("serving section paths must be unique")
    normalized_sections = [
        {
            "path": section_paths[str(section["section_id"])],
            "parent_path": _section_path(
                section.get("parent_section_id"), section_paths
            ),
            "title": section.get("section_title"),
            "level": section.get("section_level"),
            "summary": section.get("summary"),
            "sort_order": section.get("sort_order"),
        }
        for section in raw_sections
        if isinstance(section, Mapping)
    ]
    raw_chunks = payload.get("chunks") or []
    if not isinstance(raw_chunks, list):
        raise StateSnapshotError("serving chunks must be a list")
    normalized_chunks = [
        {
            "chunk_id": chunk.get("chunk_id"),
            "section_path": _section_path(chunk.get("section_id"), section_paths),
            "chunk_type": chunk.get("chunk_type"),
            "sort_order": chunk.get("sort_order"),
            "connect_to": chunk.get("connect_to"),
        }
        for chunk in raw_chunks
        if isinstance(chunk, Mapping)
    ]
    if len(normalized_chunks) != len(raw_chunks):
        raise StateSnapshotError("serving chunks must contain objects")
    raw_remounted = payload.get("remounted_assets_by_section") or {}
    if not isinstance(raw_remounted, Mapping):
        raise StateSnapshotError("remounted assets must be an object")
    return {
        "document_name": _document_name(payload.get("document_id"), document_names),
        "source_file_name": payload.get("source_file_name"),
        "sections": sorted(normalized_sections, key=_canonical_json),
        "chunks": sorted(normalized_chunks, key=_canonical_json),
        "root_asset_ids": sorted(payload.get("root_asset_ids") or []),
        "remounted_assets_by_section": {
            _section_path(section_id, section_paths): sorted(targets)
            for section_id, targets in raw_remounted.items()
        },
    }


def capture_semantic_fingerprints(
    db: Session,
    *,
    user_id: str,
    namespace: str,
) -> dict[str, str]:
    """Hash publication-owned state after the measured transaction completes.

    Raw values are used only in memory. Generated primary keys and timestamps
    are excluded; generated section references are mapped to section paths.
    """
    from shared.services.retrieval.serving_manifest import (
        decode_namespace_map_snapshot,
        decode_serving_manifest,
    )

    parameters = {"user_id": user_id, "namespace": namespace}
    documents = _rows(
        db,
        "SELECT document_id, source_file_name, status, current_job_result_id "
        "FROM documents WHERE user_id = :user_id AND namespace = :namespace",
        parameters,
    )
    document_names = {
        str(row["document_id"]): str(row["source_file_name"] or "") for row in documents
    }
    if len(set(document_names.values())) != len(document_names):
        raise StateSnapshotError(
            "semantic parity requires distinct source filenames per scope"
        )
    revision_rows = _rows(
        db,
        "SELECT jr.id, jr.document_id, jr.created_at FROM job_results jr "
        "JOIN documents d ON d.document_id = jr.document_id "
        "WHERE d.user_id = :user_id AND d.namespace = :namespace "
        "ORDER BY jr.document_id, jr.created_at, jr.id",
        parameters,
    )
    revision_keys = {
        str(row["id"]): (
            _document_name(row["document_id"], document_names),
            revision_index,
        )
        for revision_index, row in enumerate(revision_rows)
    }
    sections = _rows(
        db,
        "SELECT s.document_id, s.job_result_id, s.section_id, "
        "s.parent_section_id, s.section_path, s.section_title, "
        "s.section_level, s.summary, s.section_metadata, s.sort_order "
        "FROM document_sections s JOIN documents d ON d.document_id = s.document_id "
        "WHERE d.user_id = :user_id AND d.namespace = :namespace",
        parameters,
    )
    section_paths = {
        str(row["section_id"]): str(row["section_path"]) for row in sections
    }
    projections: dict[str, object] = {
        "documents": [
            {
                "source_file_name": document_names[str(row["document_id"])],
                "status": row["status"],
                "revision_key": _revision_key(
                    row["document_id"],
                    row["current_job_result_id"],
                    revision_keys,
                    document_names,
                ),
            }
            for row in documents
        ],
        "sections": [
            {
                "source_file_name": _document_name(row["document_id"], document_names),
                "revision_key": _revision_key(
                    row["document_id"], row["job_result_id"], revision_keys, document_names
                ),
                "path": row["section_path"],
                "parent_path": _section_path(row["parent_section_id"], section_paths),
                "title": row["section_title"],
                "level": row["section_level"],
                "summary": row["summary"],
                "metadata": row["section_metadata"],
                "sort_order": row["sort_order"],
            }
            for row in sections
        ],
    }
    chunks = _rows(
        db,
        "SELECT c.document_id, c.job_result_id, c.chunk_id, c.section_id, "
        "c.chunk_type, c.content, c.content_lexical_text, c.path_lexical_text, "
        "c.content_search_text, c.path_search_text, c.term_search_text, "
        "c.source_chunk_path, c.file_path, c.chunk_metadata, c.sort_order "
        "FROM document_chunks c JOIN documents d ON d.document_id = c.document_id "
        "WHERE d.user_id = :user_id AND d.namespace = :namespace",
        parameters,
    )
    projections["chunks"] = [
        {
            "source_file_name": _document_name(row["document_id"], document_names),
            "revision_key": _revision_key(
                row["document_id"], row["job_result_id"], revision_keys, document_names
            ),
            "chunk_id": row["chunk_id"],
            "section_path": _section_path(row["section_id"], section_paths),
            "chunk_type": row["chunk_type"],
            "content": row["content"],
            "content_lexical_text": row["content_lexical_text"],
            "path_lexical_text": row["path_lexical_text"],
            "content_search_text": row["content_search_text"],
            "path_search_text": row["path_search_text"],
            "term_search_text": row["term_search_text"],
            "source_chunk_path": row["source_chunk_path"],
            "file_path": row["file_path"],
            "metadata": row["chunk_metadata"],
            "sort_order": row["sort_order"],
        }
        for row in chunks
    ]
    map_units = _rows(
        db,
        "SELECT u.id, u.document_id, u.job_result_id, u.unit_id, u.section_id, "
        "u.unit_kind, u.path_token_count, u.content_token_count, "
        "u.term_search_text_lower, u.has_image, u.has_table, u.sort_order "
        "FROM document_map_units u JOIN documents d ON d.document_id = u.document_id "
        "WHERE d.user_id = :user_id AND d.namespace = :namespace",
        parameters,
    )
    unit_keys = {
        str(row["id"]): (
            _document_name(row["document_id"], document_names),
            _revision_key(
                row["document_id"], row["job_result_id"], revision_keys, document_names
            ),
            int(row["sort_order"]),
            str(row["unit_kind"]),
            _section_path(row["section_id"], section_paths),
        )
        for row in map_units
    }
    if len(set(unit_keys.values())) != len(unit_keys):
        raise StateSnapshotError("map units require unique semantic identities")
    projections["map_units"] = [
        {
            "unit_key": unit_keys[str(row["id"])],
            "unit_id": _unit_id(row["unit_id"], section_paths),
            "path_token_count": row["path_token_count"],
            "content_token_count": row["content_token_count"],
            "term_search_text_lower": row["term_search_text_lower"],
            "has_image": row["has_image"],
            "has_table": row["has_table"],
        }
        for row in map_units
    ]
    token_rows = _rows(
        db,
        "SELECT t.map_unit_id, t.channel, t.token, t.token_hash, t.frequency "
        "FROM document_map_unit_tokens t JOIN document_map_units u "
        "ON u.id = t.map_unit_id JOIN documents d ON d.document_id = u.document_id "
        "WHERE d.user_id = :user_id AND d.namespace = :namespace",
        parameters,
    )
    projections["map_unit_tokens"] = [
        {
            "unit_key": unit_keys[str(row["map_unit_id"])],
            "channel": row["channel"],
            "token": row["token"],
            "token_hash": row["token_hash"],
            "frequency": row["frequency"],
        }
        for row in token_rows
    ]
    indexes = _rows(
        db,
        "SELECT i.document_id, i.job_result_id, i.format_version, "
        "i.unit_count, i.token_count, i.average_idf_path, "
        "i.average_idf_content, i.path_document_count, i.path_total_length, "
        "i.content_document_count, i.content_total_length "
        "FROM document_map_unit_indexes i JOIN documents d "
        "ON d.document_id = i.document_id "
        "WHERE d.user_id = :user_id AND d.namespace = :namespace",
        parameters,
    )
    projections["map_unit_indexes"] = [
        {
            "source_file_name": _document_name(row["document_id"], document_names),
            **{
                key: value
                for key, value in row.items()
                if key not in {"document_id", "job_result_id"}
            },
            "revision_key": _revision_key(
                row["document_id"], row["job_result_id"], revision_keys, document_names
            ),
        }
        for row in indexes
    ]
    graph_nodes = _rows(
        db,
        "SELECT node_id, node_kind, owner_document_id, job_result_id, "
        "ref_document_id, ref_section_id, properties FROM graph_nodes "
        "WHERE user_id = :user_id AND namespace = :namespace",
        parameters,
    )
    node_keys = {
        str(row["node_id"]): (
            _document_name(row["owner_document_id"], document_names),
            str(row["node_kind"]),
            _document_name(row["ref_document_id"], document_names),
            _section_path(row["ref_section_id"], section_paths),
        )
        for row in graph_nodes
    }
    projections["graph_nodes"] = [
        {
            "node_key": node_keys[str(row["node_id"])],
            "properties": row["properties"],
            "revision_key": _revision_key(
                row["owner_document_id"],
                row["job_result_id"],
                revision_keys,
                document_names,
            ),
        }
        for row in graph_nodes
    ]
    graph_edges = _rows(
        db,
        "SELECT edge_kind, source_node_id, target_node_id, owner_document_id, "
        "job_result_id, is_directed, weight, properties FROM graph_edges "
        "WHERE user_id = :user_id AND namespace = :namespace",
        parameters,
    )
    projections["graph_edges"] = [
        {
            "edge_kind": row["edge_kind"],
            "source_node": node_keys[str(row["source_node_id"])],
            "target_node": node_keys[str(row["target_node_id"])],
            "owner_document": _document_name(row["owner_document_id"], document_names),
            "revision_key": _revision_key(
                row["owner_document_id"],
                row["job_result_id"],
                revision_keys,
                document_names,
            ),
            "is_directed": row["is_directed"],
            "weight": row["weight"],
            "properties": row["properties"],
        }
        for row in graph_edges
    ]
    manifest_rows = _rows(
        db,
        "SELECT m.document_id, m.job_result_id, m.format_version, m.checksum, "
        "m.payload_zlib FROM retrieval_serving_revision_manifests m "
        "JOIN documents d ON d.document_id = m.document_id "
        "WHERE d.user_id = :user_id AND d.namespace = :namespace",
        parameters,
    )
    projections["serving_manifests"] = [
        {
            "source_file_name": _document_name(row["document_id"], document_names),
            "revision_key": _revision_key(
                row["document_id"], row["job_result_id"], revision_keys, document_names
            ),
            "format_version": row["format_version"],
            "payload": normalize_serving_payload(
                decode_serving_manifest(
                    row["payload_zlib"],
                    checksum=str(row["checksum"]),
                    format_version=int(row["format_version"]),
                ),
                document_names=document_names,
            ),
        }
        for row in manifest_rows
    ]
    snapshot_rows = _rows(
        db,
        "SELECT generation, format_version, checksum, payload_zlib "
        "FROM retrieval_namespace_map_snapshots "
        "WHERE user_id = :user_id AND namespace = :namespace",
        parameters,
    )
    projections["namespace_snapshots"] = [
        _normalize_namespace_snapshot(
            decode_namespace_map_snapshot(
                row["payload_zlib"],
                checksum=str(row["checksum"]),
                format_version=int(row["format_version"]),
            ),
            document_names=document_names,
            generation=int(row["generation"]),
            format_version=int(row["format_version"]),
        )
        for row in snapshot_rows
    ]
    return {
        "schema_version": SEMANTIC_STATE_SCHEMA_VERSION,
        **{
            name: canonical_semantic_digest(sorted(value, key=_canonical_json))
            for name, value in projections.items()
            if isinstance(value, list)
        },
    }


def _normalize_namespace_snapshot(
    payload: Mapping[str, Any],
    *,
    document_names: Mapping[str, str],
    generation: int,
    format_version: int,
) -> dict[str, Any]:
    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, Mapping):
        raise StateSnapshotError("namespace snapshot documents must be an object")
    if not all(isinstance(document, Mapping) for document in raw_documents.values()):
        raise StateSnapshotError("namespace snapshot document must be an object")
    return {
        "generation": generation,
        "format_version": format_version,
        "documents": sorted(
            [
                normalize_serving_payload(
                    {**document, "document_id": document_id},
                    document_names=document_names,
                )
                for document_id, document in raw_documents.items()
                if isinstance(document, Mapping)
            ],
            key=_canonical_json,
        ),
    }


def _document_name(value: object, names: Mapping[str, str]) -> str | None:
    if value is None:
        return None
    name = names.get(str(value))
    if name is None:
        raise StateSnapshotError("document reference is outside the publication scope")
    return name


def _revision_key(
    document_id: object,
    job_result_id: object,
    revision_keys: Mapping[str, tuple[str | None, int]],
    document_names: Mapping[str, str],
) -> tuple[str | None, int]:
    """Return a stable per-document revision ordinal for semantic parity."""
    if job_result_id is None:
        raise StateSnapshotError("revision reference is missing")
    key = revision_keys.get(str(job_result_id))
    if key is None:
        raise StateSnapshotError("revision reference is outside the publication scope")
    expected_document = _document_name(document_id, document_names)
    if key[0] != expected_document:
        raise StateSnapshotError("revision reference belongs to another document")
    return key


def _section_path(value: object, paths: Mapping[str, str]) -> str | None:
    if value is None:
        return None
    path = paths.get(str(value))
    if path is None:
        raise StateSnapshotError("section reference has no matching section path")
    return path


def _unit_id(value: object, section_paths: Mapping[str, str]) -> str:
    raw = str(value)
    if raw.endswith("__self"):
        path = _section_path(raw[:-6], section_paths)
        return f"{path}__self"
    path = _section_path(raw, section_paths)
    return str(path)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _namespace_snapshot_row(
    db: Session,
    parameters: Mapping[str, Any],
) -> dict[str, Any] | None:
    rows = _rows(
        db,
        "SELECT generation, format_version, checksum, "
        "length(payload_zlib) AS payload_bytes FROM "
        "retrieval_namespace_map_snapshots WHERE user_id = :user_id AND "
        "namespace = :namespace",
        parameters,
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "generation": int(row["generation"]),
        "format_version": int(row["format_version"]),
        "checksum": str(row["checksum"]),
        "payload_bytes": int(row["payload_bytes"]),
    }


def _scalar(db: Session, statement: str, parameters: Mapping[str, Any]) -> int:
    value = db.execute(text(statement), dict(parameters)).scalar()
    return int(value or 0)


def _optional_scalar(
    db: Session,
    statement: str,
    parameters: Mapping[str, Any],
) -> int | None:
    value = db.execute(text(statement), dict(parameters)).scalar()
    return None if value is None else int(value)


def _rows(
    db: Session,
    statement: str,
    parameters: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    result = db.execute(text(statement), dict(parameters))
    return [dict(row._mapping) for row in result]
