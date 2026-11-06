"""Durable capacity charges for independent offline snapshot verification."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class LexicalOfflineReservation(Base):
    __tablename__ = "lexical_offline_reservations"

    targetClusterUuid: Mapped[str] = mapped_column(
        "target_cluster_uuid", String(64), primary_key=True
    )
    indexName: Mapped[str] = mapped_column("index_name", String(255), primary_key=True)
    targetIncarnation: Mapped[str] = mapped_column(
        "target_incarnation", String(64), nullable=False
    )
    sourceClusterUuid: Mapped[str] = mapped_column(
        "source_cluster_uuid", String(64), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    operationId: Mapped[str] = mapped_column("operation_id", String(36), nullable=False)
    issuerToken: Mapped[str] = mapped_column(
        "issuer_token",
        String(36),
        ForeignKey(
            "lexical_operator_invocations.invocation_token",
            name="fk_lexical_offline_issuer",
        ),
        nullable=False,
    )
    snapshotUuid: Mapped[str] = mapped_column(
        "snapshot_uuid", String(64), nullable=False
    )
    batchReservationId: Mapped[str] = mapped_column(
        "batch_reservation_id", String(36), nullable=False
    )
    inventorySha256: Mapped[str] = mapped_column(
        "inventory_sha256", String(64), nullable=False
    )
    boundarySha256: Mapped[str] = mapped_column(
        "boundary_sha256", String(64), nullable=False
    )
    indexUuid: Mapped[str | None] = mapped_column(
        "index_uuid", String(64), nullable=True
    )
    createdAt: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('opening','open','closing','uncertain','closed')",
            name="ck_lexical_offline_state",
        ),
        CheckConstraint(
            "target_cluster_uuid <> source_cluster_uuid",
            name="ck_lexical_offline_distinct_target",
        ),
    )
