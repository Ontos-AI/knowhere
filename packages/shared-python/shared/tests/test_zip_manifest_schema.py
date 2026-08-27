from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.storage.zip_manifest_schema import (
    ZipManifestBuilder,
    enrich_manifest_with_token_cost_estimate,
    strip_manifest_cost_fields,
)


def test_generate_manifest_omits_llm_cost_estimate() -> None:
    manifest = ZipManifestBuilder().generate_manifest(
        job_id="job_test",
        data_id="data_test",
        source_file_name="sample.pdf",
        statistics={"total_chunks": 1},
        job_metadata={
            "page_count": 10,
            "billing_status": "charged",
            "billing_amount_micro_dollars": 150_000,
            "billing_credits": 0.15,
            "stages": {
                "token_usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "calls": 2,
                    "by_model": {"deepseek-chat": {"total_tokens": 120, "calls": 2}},
                }
            },
        },
        hierarchy={"Root": {}},
    )

    processing = manifest["processing"]
    assert "cost_estimate" not in processing
    assert processing["cost"]["credits"] == 0.15
    assert processing["stages"]["token_usage"]["by_model"]


def test_enrich_and_strip_manifest_cost_fields() -> None:
    manifest = ZipManifestBuilder().generate_manifest(
        job_id="job_test",
        data_id=None,
        source_file_name="sample.pdf",
        statistics={},
        job_metadata={
            "stages": {
                "token_usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                    "calls": 1,
                    "by_model": {},
                    "by_task": {},
                }
            }
        },
    )

    enriched = enrich_manifest_with_token_cost_estimate(manifest)
    assert "cost_estimate" in enriched["processing"]
    assert enriched["processing"]["cost_estimate"]["currency"] == "USD"

    stripped = strip_manifest_cost_fields(enriched)
    assert "cost_estimate" not in stripped["processing"]
    assert stripped["processing"]["stages"]["token_usage"]["total_tokens"] == 14
