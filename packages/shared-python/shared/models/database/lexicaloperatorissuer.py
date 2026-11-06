"""Physical operator invocation ownership; elapsed time never releases authority."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class LexicalOperatorIssuer(Base):
    __tablename__ = "lexical_operator_issuers"

    logicalClusterId: Mapped[str] = mapped_column("logical_cluster_id", String(128), primary_key=True)
    controlId: Mapped[int] = mapped_column("control_id", Integer,
        ForeignKey("lexical_backend_control.id", name="fk_lexical_operator_control"),
        nullable=False, default=1, server_default="1")
    invocationToken: Mapped[str] = mapped_column("invocation_token", String(36), nullable=False)
    hostIdentity: Mapped[str] = mapped_column("host_identity", String(64), nullable=False)
    operationId: Mapped[str] = mapped_column("operation_id", String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    sourceHash: Mapped[str] = mapped_column("source_hash", String(64), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    clusterUuid: Mapped[str] = mapped_column("cluster_uuid", String(64), nullable=False)
    acquiredAt: Mapped[datetime] = mapped_column("acquired_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    releasedAt: Mapped[datetime | None] = mapped_column("released_at", DateTime(timezone=True), nullable=True)
    fenceReceiptSha256: Mapped[str | None] = mapped_column("fence_receipt_sha256", String(64), nullable=True)
    __table_args__ = (
        UniqueConstraint("control_id", name="uq_lexical_operator_control"),
        UniqueConstraint("invocation_token", name="uq_lexical_operator_invocation"),
        CheckConstraint("state IN ('active', 'unknown', 'released')", name="ck_lexical_operator_issuer_state"),
        CheckConstraint("epoch >= 1", name="ck_lexical_operator_issuer_epoch"),
    )
