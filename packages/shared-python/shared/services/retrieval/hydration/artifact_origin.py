"""Resolve claim-owned assets while a revision still serves its baseline."""

from collections.abc import Mapping

from shared.services.storage.raw_prefix_arguments import RawPrefixArguments
from shared.services.storage.result_storage import ResultStorage


def resolve_artifact_raw_prefix(
    row: Mapping[str, object],
    artifact_ref: str,
    *,
    storage: ResultStorage,
) -> RawPrefixArguments:
    """Use persisted per-asset origins only inside the owning job's result tree."""
    prefix: object = row.get("result_raw_prefix")
    metadata: object = row.get("chunk_metadata") or row.get("metadata")
    origins: object = (
        metadata.get("artifact_origins") if isinstance(metadata, dict) else None
    )
    origin: object = origins.get(artifact_ref) if isinstance(origins, dict) else None
    if origin is not None:
        job_id: str = str(row.get("job_id") or "")
        canonical_key: str = storage.build_raw_key(
            job_id=job_id, relative_path=artifact_ref
        )
        canonical_prefix: str = canonical_key.removesuffix(artifact_ref)
        if (
            not isinstance(origin, str)
            or not canonical_prefix
            or not origin.startswith(canonical_prefix)
            or not origin.endswith("/")
            or any(part in {".", ".."} for part in origin.split("/"))
        ):
            raise ValueError("Artifact origin must remain inside its owning job")
        snapshot_prefix: object = (
            metadata.get("artifact_origin_snapshot_prefix")
            if isinstance(metadata, dict)
            else None
        )
        # Final publication switches the revision prefix atomically. Its package
        # contains every asset, so reads then use that retained snapshot.
        if snapshot_prefix is None or snapshot_prefix == prefix:
            return RawPrefixArguments(raw_prefix=origin)
    return (
        RawPrefixArguments(raw_prefix=str(prefix)) if prefix else RawPrefixArguments()
    )
