"""Reference projection for decomposed retrieval workflows."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class WorkflowReferenceProjection:
    def dedupe(self, refs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for ref in refs:
            document_id = str(ref.get("document_id") or "").strip()
            chunk_id = str(ref.get("chunk_id") or "").strip()
            section_path = str(ref.get("section_path") or "").strip()
            file_path = str(ref.get("file_path") or "").strip()
            key = (
                f"{document_id}:{chunk_id}:{section_path}:{file_path}"
                if document_id and chunk_id
                else str(ref)
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(ref))
        return out

    def to_api_results(self, refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "chunk_id": ref.get("chunk_id"),
                "document_id": ref.get("document_id"),
                "chunk_type": ref.get("chunk_type"),
                "source": {
                    "document_id": ref.get("document_id"),
                    "section_path": ref.get("section_path"),
                },
            }
            for ref in refs
        ]
