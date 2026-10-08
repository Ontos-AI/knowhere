"""Sign only retained, published demo assets without creating derivative files."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote

from shared.services.storage.job_file_storage import JobFileStorage
from shared.services.storage.result_storage import get_result_storage


@dataclass(frozen=True)
class DemoAssetSigner:
    job_id: str
    revision_metadata: Mapping[str, object]

    def generate_url(self, artifact_ref: str) -> str | None:
        storage = get_result_storage()
        normalized: str | None = (
            "source.pdf"
            if artifact_ref == "source.pdf"
            else storage.normalize_artifact_ref(artifact_ref)
        )
        allowed_assets: object = self.revision_metadata.get("demo_asset_manifest")
        if (
            normalized != artifact_ref
            or not isinstance(allowed_assets, list)
            or artifact_ref not in allowed_assets
        ):
            return None
        raw_prefix: object = self.revision_metadata.get("result_raw_prefix")
        key: str = storage.build_raw_key(
            job_id=self.job_id,
            relative_path=artifact_ref,
            raw_prefix=str(raw_prefix) if raw_prefix else None,
        )
        file_name: str = artifact_ref.rsplit("/", 1)[-1]
        mime_type: str = (
            mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        )
        files: JobFileStorage = JobFileStorage()
        return files.storage_adapter.generate_presigned_url(
            key,
            expiration=3600,
            bucket=files.results_bucket,
            method="GET",
            headers={
                "Content-Type": mime_type,
                "Content-Disposition": "inline; filename*=UTF-8''"
                + quote(file_name, safe=""),
            },
        )
