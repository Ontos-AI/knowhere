from __future__ import annotations

from shared.services.storage.result_storage import get_result_storage


async def generate_retrieval_asset_url(*, job_id: str, artifact_ref: str) -> str | None:
    return get_result_storage().generate_artifact_url(
        job_id=job_id,
        artifact_ref=artifact_ref,
    )


def is_client_result_artifact_ref(asset_ref: str | None) -> bool:
    return get_result_storage().normalize_artifact_ref(asset_ref) is not None
