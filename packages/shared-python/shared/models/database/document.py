"""Canonical retrieval-serving document state models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import (
    Computed,
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive


class Document(Base):
    """Durable user document in a retrieval namespace."""

    __tablename__ = "documents"

    document_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: f"doc_{uuid4().hex[:12]}"
    )
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    namespace: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    current_job_result_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("job_results.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    source_file_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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

    sections: Mapped[List["DocumentSection"]] = relationship(
        "DocumentSection",
        back_populates="document",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    chunks: Mapped[List["DocumentChunk"]] = relationship(
        "DocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    __table_args__ = (
        Index("idx_documents_user_namespace_status", "user_id", "namespace", "status"),
        Index("idx_documents_current_job_result", "current_job_result_id"),
    )


class DocumentSection(Base):
    """Canonical hierarchy node for one published document revision."""

    __tablename__ = "document_sections"

    section_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: f"sec_{uuid4().hex[:12]}"
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default"
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    job_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("job_results.id", ondelete="CASCADE"), nullable=False
    )
    parent_section_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("document_sections.section_id", ondelete="SET NULL"),
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

    document: Mapped[Document] = relationship("Document", back_populates="sections")

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "job_result_id",
            "section_path",
            name="uq_document_sections_revision_path",
        ),
        Index("idx_document_sections_scope", "user_id", "namespace"),
        Index("idx_document_sections_doc_revision", "document_id", "job_result_id"),
    )


class DocumentChunk(Base):
    """Canonical retrieval payload row for one published document revision."""

    __tablename__ = "document_chunks"

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
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    job_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("job_results.id", ondelete="CASCADE"), nullable=False
    )
    section_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("document_sections.section_id", ondelete="SET NULL"),
        nullable=True,
    )
    chunk_type: Mapped[str] = mapped_column(String(64), nullable=False, default="text")
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_lexical_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    path_lexical_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_search_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    path_search_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    term_search_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_search_tsv: Mapped[Optional[str]] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', COALESCE(content_search_text, ''))",
            persisted=True,
        ),
        nullable=True,
    )
    path_search_tsv: Mapped[Optional[str]] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', COALESCE(path_search_text, ''))",
            persisted=True,
        ),
        nullable=True,
    )
    source_chunk_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    file_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    chunk_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )

    document: Mapped[Document] = relationship("Document", back_populates="chunks")

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "job_result_id",
            "source_chunk_path",
            name="uq_document_chunks_revision_path",
        ),
        Index("idx_document_chunks_scope", "user_id", "namespace"),
        Index("idx_document_chunks_chunk_id", "chunk_id"),
        Index("idx_document_chunks_doc_revision", "document_id", "job_result_id"),
        Index("idx_document_chunks_section", "section_id"),
        Index(
            "idx_chunk_content_search_tsv",
            "content_search_tsv",
            postgresql_using="gin",
        ),
        Index(
            "idx_chunk_path_search_tsv",
            "path_search_tsv",
            postgresql_using="gin",
        ),
    )


class GraphNode(Base):
    """Persisted derived graph node used for routing and expansion."""

    __tablename__ = "graph_nodes"

    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default"
    )
    node_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    job_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("job_results.id", ondelete="CASCADE"), nullable=False
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
        Index("idx_graph_nodes_scope", "user_id", "namespace", "node_kind"),
        Index("idx_graph_nodes_owner_revision", "owner_document_id", "job_result_id"),
        Index("idx_graph_nodes_ref_document", "ref_document_id"),
        Index("idx_graph_nodes_ref_section", "ref_section_id"),
    )


class GraphEdge(Base):
    """Persisted derived graph edge used for routing and expansion."""

    __tablename__ = "graph_edges"

    edge_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default"
    )
    edge_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_node_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("graph_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
    )
    target_node_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("graph_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    job_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("job_results.id", ondelete="CASCADE"), nullable=False
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
        Index("idx_graph_edges_scope", "user_id", "namespace", "edge_kind"),
        Index("idx_graph_edges_owner_revision", "owner_document_id", "job_result_id"),
        Index("idx_graph_edges_source", "source_node_id"),
        Index("idx_graph_edges_target", "target_node_id"),
    )


class RetrievalHitStat(Base):
    """Append-only retrieval usage analytics row."""

    __tablename__ = "retrieval_hit_stats"

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
        ForeignKey("documents.document_id", ondelete="CASCADE"),
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
            "uq_retrieval_hit_stats_document_key",
            "user_id",
            "namespace",
            "hit_kind",
            "document_id",
            unique=True,
            postgresql_where=chunk_id.is_(None),
        ),
        Index(
            "uq_retrieval_hit_stats_chunk_key",
            "user_id",
            "namespace",
            "hit_kind",
            "document_id",
            "chunk_id",
            unique=True,
            postgresql_where=chunk_id.is_not(None),
        ),
        Index("idx_retrieval_hit_stats_scope_kind", "user_id", "namespace", "hit_kind"),
        Index("idx_retrieval_hit_stats_document", "document_id"),
        Index("idx_retrieval_hit_stats_chunk", "chunk_id"),
    )


class RetrievalRun(Base):
    """One row per agentic retrieval query.  Append-only analytics."""

    __tablename__ = 'retrieval_runs'

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: f'aret_{uuid4().hex[:12]}')
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False, default='default')
    query: Mapped[str] = mapped_column(Text, nullable=False)
    query_hash: Mapped[str] = mapped_column(String(32), nullable=False, default='')
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    data_type: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    filters: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    policy_name: Mapped[str] = mapped_column(String(64), nullable=False, default='rule_based_v1')
    agentic_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    cache_hit: Mapped[bool] = mapped_column(nullable=False, default=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    final_doc_ids: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    result_provenance: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    parent_run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    workflow_step_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    workflow_plan: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index('idx_retrieval_runs_user_namespace', 'user_id', 'namespace'),
        Index('idx_retrieval_runs_created', 'created_at'),
        Index('idx_retrieval_runs_query_hash', 'query_hash'),
    )


class RetrievalStep(Base):
    """One row per agent step within a retrieval run.  Append-only analytics."""

    __tablename__ = 'retrieval_steps'

    step_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: f'arst_{uuid4().hex[:12]}')
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    action_input: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    observation: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    selected_doc_ids: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    selected_paths: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index('idx_retrieval_steps_run', 'run_id', 'step_index'),
    )
