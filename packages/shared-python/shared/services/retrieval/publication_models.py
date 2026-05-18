from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PublishedDocumentState:
    user_id: str
    namespace: str
    document_id: str | None
    skipped_all_duplicate: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "user_id": self.user_id,
            "namespace": self.namespace,
            "document_id": self.document_id,
        }
        if self.skipped_all_duplicate:
            payload["skipped_all_duplicate"] = True
        return payload


@dataclass(frozen=True)
class DocumentPublicationScope:
    user_id: str
    namespace: str
    document_id: str
    job_result_id: str
    source_file_name: str | None
