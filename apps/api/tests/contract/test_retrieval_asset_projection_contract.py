from __future__ import annotations

from typing import Any

import pytest


class FakeResultStorage:
    def __init__(self) -> None:
        self.should_fail: bool = False
        self.generated_refs: list[tuple[str, str]] = []

    def normalize_artifact_ref(self, artifact_ref: str | None) -> str | None:
        if not artifact_ref:
            return None
        normalized = str(artifact_ref).strip().replace("\\", "/").lstrip("/")
        if normalized.startswith("images/") or normalized.startswith("tables/"):
            return normalized
        return None

    def generate_artifact_url(
        self,
        *,
        job_id: str,
        artifact_ref: str,
        expires_in: int = 3600,
    ) -> str | None:
        del expires_in
        if self.should_fail:
            raise RuntimeError("storage signing failed")
        normalized = self.normalize_artifact_ref(artifact_ref)
        if normalized is None:
            return None
        self.generated_refs.append((job_id, normalized))
        return f"https://assets.example.test/{job_id}/{normalized}"


@pytest.mark.asyncio
async def test_retrieval_asset_projection_should_attach_signed_urls_only_to_result_media_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.assets import enrich_rows_with_retrieval_asset_urls

    fake_storage = FakeResultStorage()
    monkeypatch.setattr(
        "shared.services.retrieval.assets.get_result_storage",
        lambda: fake_storage,
    )

    rows: list[dict[str, Any]] = [
        {
            "chunk_id": "image-chunk",
            "chunk_type": "image",
            "job_id": "job_123",
            "file_path": "images/chart.png",
        },
        {
            "chunk_id": "text-chunk",
            "chunk_type": "text",
            "job_id": "job_123",
            "file_path": "images/inline.png",
        },
        {
            "chunk_id": "external-chunk",
            "chunk_type": "table",
            "job_id": "job_123",
            "file_path": "https://example.test/table.html",
        },
    ]

    enriched_rows = await enrich_rows_with_retrieval_asset_urls(
        rows,
        log_context="contract projection",
    )

    assert enriched_rows[0]["asset_url"] == (
        "https://assets.example.test/job_123/images/chart.png"
    )
    assert "asset_url" not in enriched_rows[1]
    assert "asset_url" not in enriched_rows[2]
    assert fake_storage.generated_refs == [("job_123", "images/chart.png")]


@pytest.mark.asyncio
async def test_retrieval_asset_projection_should_build_chunk_url_map_and_ignore_storage_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.assets import build_retrieval_asset_url_map

    fake_storage = FakeResultStorage()
    monkeypatch.setattr(
        "shared.services.retrieval.assets.get_result_storage",
        lambda: fake_storage,
    )

    url_map = await build_retrieval_asset_url_map(
        [
            {
                "chunk_id": "image-chunk",
                "type": "image",
                "job_id": "job_123",
                "file_path": "images/chart.png",
            },
        ],
        log_context="contract map",
    )

    fake_storage.should_fail = True
    failed_url_map = await build_retrieval_asset_url_map(
        [
            {
                "chunk_id": "table-chunk",
                "type": "table",
                "job_id": "job_123",
                "file_path": "tables/data.html",
            },
        ],
        log_context="contract map",
    )

    assert url_map == {
        "image-chunk": "https://assets.example.test/job_123/images/chart.png"
    }
    assert failed_url_map == {}
