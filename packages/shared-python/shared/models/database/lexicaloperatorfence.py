"""A durable one-shot physical fence intent for each old invocation."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class LexicalOperatorFence(Base):
    __tablename__ = "lexical_operator_fences"

    oldInvocationToken: Mapped[str] = mapped_column("old_invocation_token", String(36),
        ForeignKey("lexical_operator_invocations.invocation_token", name="fk_lexical_fence_old_invocation"), primary_key=True)
    fenceToken: Mapped[str] = mapped_column("fence_token", String(36), nullable=False)
    oldBindingSha256: Mapped[str] = mapped_column("old_binding_sha256", String(64), nullable=False)
    sourceHash: Mapped[str] = mapped_column("source_hash", String(64), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    clusterUuid: Mapped[str] = mapped_column("cluster_uuid", String(64), nullable=False)
    controlBackend: Mapped[str] = mapped_column("control_backend", String(32), nullable=False)
    nodeIncarnation: Mapped[str | None] = mapped_column("node_incarnation", String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    reservedAt: Mapped[datetime] = mapped_column("reserved_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    proofSha256: Mapped[str | None] = mapped_column("proof_sha256", String(64), nullable=True)
    provenAt: Mapped[datetime | None] = mapped_column("proven_at", DateTime(timezone=True), nullable=True)
    consumedAt: Mapped[datetime | None] = mapped_column("consumed_at", DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("fence_token", name="uq_lexical_fence_token"),
        CheckConstraint("epoch >= 1", name="ck_lexical_fence_epoch"),
        CheckConstraint("(state='pending' AND proof_sha256 IS NULL AND proven_at IS NULL AND consumed_at IS NULL)"
            " OR (state='proven' AND proof_sha256 IS NOT NULL AND proven_at IS NOT NULL AND consumed_at IS NULL)"
            " OR (state='consumed' AND proof_sha256 IS NOT NULL AND proven_at IS NOT NULL AND consumed_at IS NOT NULL)",
            name="ck_lexical_fence_progress"),
    )
