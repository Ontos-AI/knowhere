"""Dedicated shared demo persistence records; private tables remain isolated."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKeyConstraint,
    DateTime,
    Float,
    ForeignKey,
    BigInteger,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive


class DemoDocument(Base):
    """Shared source directory and its current complete publication revision."""

    __tablename__ = "demo_documents"

    document_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: f"ddoc_{uuid4().hex[:12]}"
    )
    demo_source_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default="other"
    )
    catalog_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )

    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(
        String(255), nullable=False, default="__knowhere_demo__"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    current_job_result_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("job_results.id", ondelete="RESTRICT", use_alter=True),
        nullable=True,
    )
    source_file_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    document_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    parse_track: Mapped[str] = mapped_column(
        String(32), nullable=False, default="chunk"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now_naive,
        onupdate=utc_now_naive,
        nullable=False,
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["current_job_result_id", "document_id"],
            ["job_results.id", "job_results.demo_document_id"],
            name="fk_demo_documents_current_revision",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            "namespace = '__knowhere_demo__'", name="ck_demo_documents_namespace"
        ),
        Index(
            "idx_demo_documents_user_namespace_status", "user_id", "namespace", "status"
        ),
        Index("idx_demo_documents_current_job_result", "current_job_result_id"),
    )


class DemoDocumentSection(Base):
    """Canonical hierarchy node for one published document revision."""

    __tablename__ = "demo_document_sections"

    section_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: f"sec_{uuid4().hex[:12]}"
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default"
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("demo_documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    job_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("job_results.id", ondelete="RESTRICT"), nullable=False
    )
    parent_section_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("demo_document_sections.section_id", ondelete="SET NULL"),
        nullable=True,
    )
    section_path: Mapped[str] = mapped_column(Text, nullable=False)
    section_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    section_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    section_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "section_id",
            "document_id",
            "job_result_id",
            name="uq_demo_sections_identity",
        ),
        ForeignKeyConstraint(
            ["parent_section_id", "document_id", "job_result_id"],
            [
                "demo_document_sections.section_id",
                "demo_document_sections.document_id",
                "demo_document_sections.job_result_id",
            ],
            deferrable=True,
            initially="DEFERRED",
            name="fk_demo_sections_parent_revision",
        ),
        ForeignKeyConstraint(
            ["job_result_id", "document_id"],
            ["job_results.id", "job_results.demo_document_id"],
            ondelete="RESTRICT",
            name="fk_demo_documentsection_revision",
        ),
        UniqueConstraint(
            "document_id",
            "job_result_id",
            "section_path",
            name="uq_demo_document_sections_revision_path",
        ),
        Index("idx_demo_document_sections_scope", "user_id", "namespace"),
        Index(
            "idx_demo_document_sections_doc_revision", "document_id", "job_result_id"
        ),
        Index(
            "idx_demo_document_sections_revision_snapshot_order",
            "document_id",
            "job_result_id",
            "sort_order",
            "section_id",
        ),
    )


class DemoDocumentChunk(Base):
    """Canonical retrieval payload row for one published document revision."""

    __tablename__ = "demo_document_chunks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: f"dchk_{uuid4().hex[:12]}"
    )

    chunk_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default"
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("demo_documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    job_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("job_results.id", ondelete="RESTRICT"), nullable=False
    )
    section_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("demo_document_sections.section_id", ondelete="SET NULL"),
        nullable=True,
    )
    chunk_type: Mapped[str] = mapped_column(String(64), nullable=False, default="text")
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_lexical_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    path_lexical_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_search_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    path_search_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    term_search_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_chunk_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    file_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    chunk_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["section_id", "document_id", "job_result_id"],
            [
                "demo_document_sections.section_id",
                "demo_document_sections.document_id",
                "demo_document_sections.job_result_id",
            ],
            deferrable=True,
            initially="DEFERRED",
            name="fk_demo_documentchunk_section_revision",
        ),
        ForeignKeyConstraint(
            ["job_result_id", "document_id"],
            ["job_results.id", "job_results.demo_document_id"],
            ondelete="RESTRICT",
            name="fk_demo_documentchunk_revision",
        ),
        UniqueConstraint(
            "document_id",
            "job_result_id",
            "source_chunk_path",
            name="uq_demo_document_chunks_revision_path",
        ),
        Index("idx_demo_document_chunks_scope", "user_id", "namespace"),
        Index("idx_demo_document_chunks_chunk_id", "chunk_id"),
        Index("idx_demo_document_chunks_doc_revision", "document_id", "job_result_id"),
        Index(
            "idx_demo_document_chunks_revision_snapshot_order",
            "document_id",
            "job_result_id",
            "sort_order",
            "chunk_id",
            "id",
        ),
        Index(
            "idx_demo_document_chunks_revision_section_order",
            "document_id",
            "job_result_id",
            "section_id",
            "sort_order",
            "chunk_id",
            "id",
        ),
        Index("idx_demo_document_chunks_section", "section_id"),
    )


class DemoDocumentMapUnit(Base):
    """Persisted lexical map unit for one document revision.

    These rows are a derived index of the exact leaf and interstitial units
    used by map-nav. Full chunk payloads remain in ``demo_document_chunks`` and are
    loaded separately for evidence hydration.
    """

    __tablename__ = "demo_document_map_units"

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("demo_documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    job_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("job_results.id", ondelete="RESTRICT"), nullable=False
    )
    unit_id: Mapped[str] = mapped_column(String(128), nullable=False)
    section_id: Mapped[str] = mapped_column(String(36), nullable=False)
    unit_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    path_token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Asset presence under this unit's section, after root-asset remount
    # (``KnowhereProvider._remount_root_assets``). Lets type-scoped queries
    # (e.g. chunk_types=["image"]) narrow map-unit candidates *before*
    # scoring instead of scoring everything and discarding after the fact.
    has_image: Mapped[bool] = mapped_column(nullable=False, default=False)
    has_table: Mapped[bool] = mapped_column(nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["section_id", "document_id", "job_result_id"],
            [
                "demo_document_sections.section_id",
                "demo_document_sections.document_id",
                "demo_document_sections.job_result_id",
            ],
            deferrable=True,
            initially="DEFERRED",
            name="fk_demo_documentmapunit_section_revision",
        ),
        ForeignKeyConstraint(
            ["job_result_id", "document_id"],
            ["job_results.id", "job_results.demo_document_id"],
            ondelete="RESTRICT",
            name="fk_demo_documentmapunit_revision",
        ),
        Index(
            "idx_demo_document_map_units_revision_order",
            "document_id",
            "job_result_id",
            "sort_order",
            "unit_id",
        ),
        Index("idx_demo_document_map_units_section", "section_id"),
        Index(
            "idx_demo_document_map_units_has_image",
            "document_id",
            "job_result_id",
            postgresql_where=has_image.is_(True),
        ),
        Index(
            "idx_demo_document_map_units_has_table",
            "document_id",
            "job_result_id",
            postgresql_where=has_table.is_(True),
        ),
    )


class DemoDocumentMapUnitToken(Base):
    """One exact token frequency in a persisted map unit channel."""

    __tablename__ = "demo_document_map_unit_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    map_unit_id: Mapped[str] = mapped_column(
        String(160),
        ForeignKey(
            "demo_document_map_units.id",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    frequency: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index(
            "idx_demo_document_map_unit_tokens_token_lookup_binary",
            "channel",
            text("decode(token_hash, 'hex'::text)"),
            postgresql_include=["map_unit_id", "token", "frequency"],
        ),
        Index(
            "idx_demo_document_map_unit_tokens_unit",
            "map_unit_id",
            "channel",
            postgresql_include=["token", "frequency"],
        ),
    )


class DemoDocumentMapUnitIndex(Base):
    """Completeness marker and corpus statistics for a materialized index."""

    __tablename__ = "demo_document_map_unit_indexes"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("demo_documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    job_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("job_results.id", ondelete="RESTRICT"), nullable=False
    )
    format_version: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # rank_bm25 Okapi average IDF for the revision's units (path/content).
    # Written at index time so query scoring never rescans all tokens.
    average_idf_path: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    average_idf_content: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    path_document_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    path_total_length: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_document_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    content_total_length: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["job_result_id", "document_id"],
            ["job_results.id", "job_results.demo_document_id"],
            ondelete="RESTRICT",
            name="fk_demo_documentmapunitindex_revision",
        ),
        UniqueConstraint(
            "document_id",
            "job_result_id",
            name="uq_demo_document_map_unit_indexes_revision",
        ),
        Index(
            "idx_demo_document_map_unit_indexes_revision",
            "document_id",
            "job_result_id",
        ),
    )


class DemoRetrievalNamespaceGeneration(Base):
    """Monotonic serving generation for one user-owned namespace."""

    __tablename__ = "demo_retrieval_namespace_generations"

    id: Mapped[str] = mapped_column(
        String(100), primary_key=True, default=lambda: f"rng_{uuid4().hex}"
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, onupdate=utc_now_naive, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "namespace",
            name="uq_demo_retrieval_namespace_generations_scope",
        ),
    )


class DemoRetrievalServingRevisionManifest(Base):
    """Compressed ordered metadata for one document revision."""

    __tablename__ = "demo_retrieval_serving_revision_manifests"

    id: Mapped[str] = mapped_column(
        String(100), primary_key=True, default=lambda: f"rsm_{uuid4().hex}"
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("demo_documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    job_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("job_results.id", ondelete="RESTRICT"), nullable=False
    )
    format_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_zlib: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["job_result_id", "document_id"],
            ["job_results.id", "job_results.demo_document_id"],
            ondelete="RESTRICT",
            name="fk_demo_retrievalservingrevisionmanifest_revision",
        ),
        UniqueConstraint(
            "document_id",
            "job_result_id",
            name="uq_demo_retrieval_serving_revision_manifests_revision",
        ),
        Index(
            "idx_demo_retrieval_serving_revision_manifests_scope",
            "user_id",
            "namespace",
            "document_id",
            "job_result_id",
        ),
    )


class DemoRetrievalNamespaceMapSnapshot(Base):
    """Persisted namespace-level MAP (sections + chunk index + map units).

    Incrementally patched at publish/archive time (one document's subtree at
    a time); query time reads this row directly instead of merging per-file
    manifests. Overwritten in place -- no generation history is retained.
    """

    __tablename__ = "demo_retrieval_namespace_map_snapshots"

    id: Mapped[str] = mapped_column(
        String(100), primary_key=True, default=lambda: f"rnmap_{uuid4().hex}"
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    format_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_zlib: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, onupdate=utc_now_naive, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "namespace",
            name="uq_demo_retrieval_namespace_map_snapshots_scope",
        ),
    )


class DemoGraphNode(Base):
    """Persisted derived graph node used for routing and expansion."""

    __tablename__ = "demo_graph_nodes"

    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default"
    )
    node_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("demo_documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    job_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("job_results.id", ondelete="RESTRICT"), nullable=False
    )
    ref_document_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    ref_section_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    properties: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now_naive,
        onupdate=utc_now_naive,
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["job_result_id", "owner_document_id"],
            ["job_results.id", "job_results.demo_document_id"],
            ondelete="RESTRICT",
            name="fk_demo_graphnode_revision",
        ),
        Index("idx_demo_graph_nodes_scope", "user_id", "namespace", "node_kind"),
        Index(
            "idx_demo_graph_nodes_owner_revision", "owner_document_id", "job_result_id"
        ),
        Index("idx_demo_graph_nodes_ref_document", "ref_document_id"),
        Index("idx_demo_graph_nodes_ref_section", "ref_section_id"),
        Index(
            "idx_demo_graph_nodes_top_keywords_gin",
            text("(properties::jsonb -> 'top_keywords')"),
            postgresql_using="gin",
        ),
        Index(
            "idx_demo_graph_nodes_top_entities_gin",
            text("(properties::jsonb -> 'top_entities')"),
            postgresql_using="gin",
        ),
    )


class DemoGraphEdge(Base):
    """Persisted derived graph edge used for routing and expansion."""

    __tablename__ = "demo_graph_edges"

    edge_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default"
    )
    edge_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_node_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("demo_graph_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
    )
    target_node_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("demo_graph_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("demo_documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    job_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("job_results.id", ondelete="RESTRICT"), nullable=False
    )
    is_directed: Mapped[bool] = mapped_column(nullable=False, default=True)
    weight: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    properties: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now_naive,
        onupdate=utc_now_naive,
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["job_result_id", "owner_document_id"],
            ["job_results.id", "job_results.demo_document_id"],
            ondelete="RESTRICT",
            name="fk_demo_graphedge_revision",
        ),
        Index("idx_demo_graph_edges_scope", "user_id", "namespace", "edge_kind"),
        Index(
            "idx_demo_graph_edges_owner_revision", "owner_document_id", "job_result_id"
        ),
        Index("idx_demo_graph_edges_source", "source_node_id"),
        Index("idx_demo_graph_edges_target", "target_node_id"),
    )


class DemoRetrievalHitStat(Base):
    """Append-only retrieval usage analytics row."""

    __tablename__ = "demo_retrieval_hit_stats"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: f"rhs_{uuid4().hex[:12]}"
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default"
    )
    hit_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("demo_documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_hit_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now_naive,
        onupdate=utc_now_naive,
        nullable=False,
    )

    __table_args__ = (
        Index(
            "uq_demo_retrieval_hit_stats_document_key",
            "user_id",
            "namespace",
            "hit_kind",
            "document_id",
            unique=True,
            postgresql_where=chunk_id.is_(None),
        ),
        Index(
            "uq_demo_retrieval_hit_stats_chunk_key",
            "user_id",
            "namespace",
            "hit_kind",
            "document_id",
            "chunk_id",
            unique=True,
            postgresql_where=chunk_id.is_not(None),
        ),
        Index(
            "idx_demo_retrieval_hit_stats_scope_kind",
            "user_id",
            "namespace",
            "hit_kind",
        ),
        Index("idx_demo_retrieval_hit_stats_document", "document_id"),
        Index("idx_demo_retrieval_hit_stats_chunk", "chunk_id"),
    )
