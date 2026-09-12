from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExistingDocumentScope:
    document_id: str
    namespace: str


@dataclass(frozen=True)
class PublishedDocumentState:
    user_id: str
    namespace: str
    document_id: str | None
    skipped_all_duplicate: bool = False
    manifest_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class DocumentPublicationScope:
    user_id: str
    namespace: str
    document_id: str
    job_result_id: str
    source_file_name: str | None
