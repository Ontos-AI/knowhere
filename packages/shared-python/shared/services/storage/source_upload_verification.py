from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class SourceUploadVerification:
    """Checksum evidence and server timestamps from a single storage lookup."""

    status: Literal["matched", "missing", "mismatch"]
    checked_at: datetime
    last_modified: datetime | None = None
