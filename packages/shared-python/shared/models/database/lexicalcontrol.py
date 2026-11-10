"""A global backend epoch fences stale publication and recovery work."""

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class LexicalBackendControl(Base):
    __tablename__ = "lexical_backend_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="serving")
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    clusterUuid: Mapped[str | None] = mapped_column("cluster_uuid", String(64), nullable=True)
    nodeIncarnation: Mapped[str | None] = mapped_column("node_incarnation", String(64), nullable=True)
