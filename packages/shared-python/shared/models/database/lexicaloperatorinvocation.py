"""Append-only physical invocation nonces prevent historical ownership replay."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class LexicalOperatorInvocation(Base):
    __tablename__ = "lexical_operator_invocations"

    invocationToken: Mapped[str] = mapped_column("invocation_token", String(36), primary_key=True)
    logicalClusterId: Mapped[str] = mapped_column("logical_cluster_id", String(128),
        ForeignKey("lexical_operator_issuers.logical_cluster_id", name="fk_lexical_invocation_issuer"), nullable=False)
    recordedAt: Mapped[datetime] = mapped_column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now())
