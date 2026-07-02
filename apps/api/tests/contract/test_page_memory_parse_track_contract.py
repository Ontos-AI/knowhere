from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.core.exceptions.domain_exceptions import ValidationException
from shared.models.schemas.job import JobCreate, JobCreateV2


def _build_job_create(**values: object) -> JobCreate:
    return JobCreate.model_validate(values)


def _build_job_create_v2(**values: object) -> JobCreateV2:
    return JobCreateV2.model_validate(values)


def test_v1_ingestion_command_always_resolves_chunk_for_supported_files() -> None:
    from app.services.document_ingestion.command import build_v1_ingestion_command

    command = build_v1_ingestion_command(
        payload=_build_job_create(source_type="file", file_name="policy.pdf"),
    )

    assert command.api_version == "v1"
    assert command.parse_track == "chunk"
    assert command.processing_generation == "legacy_chunk"
    assert command.page_memory_config is None


@pytest.mark.parametrize("file_extension", [".pdf", ".pptx"])
def test_v2_ingestion_command_resolves_page_memory_for_current_v2_formats(
    file_extension: str,
) -> None:
    from app.services.document_ingestion.command import build_v2_ingestion_command

    command = build_v2_ingestion_command(
        payload=_build_job_create_v2(
            source_type="file",
            file_name=f"policy{file_extension}",
        ),
        file_extension=file_extension,
    )

    assert command.api_version == "v2"
    assert command.parse_track == "page_memory"
    assert command.processing_generation == "page_memory"
    assert command.page_memory_config is not None
    assert command.page_memory_config.max_pages == 1500


def test_v2_ingestion_command_uses_chunk_for_future_v2_formats_until_supported() -> None:
    from app.services.document_ingestion.command import build_v2_ingestion_command

    command = build_v2_ingestion_command(
        payload=_build_job_create_v2(source_type="file", file_name="policy.docx"),
        file_extension=".docx",
    )

    assert command.api_version == "v2"
    assert command.parse_track == "chunk"
    assert command.processing_generation == "legacy_chunk"
    assert command.page_memory_config is None


def test_v1_accepts_deprecated_chunk_parse_track_as_noop() -> None:
    from app.services.document_ingestion.service import (
        _validate_public_mode_selector_fields,
    )
    from shared.models.schemas.job_metadata import JobMetadataHelper

    payload = _build_job_create(
        source_type="file",
        file_name="policy.pdf",
        parse_track="chunk",
    )

    _validate_public_mode_selector_fields(payload, api_version="v1")
    metadata = JobMetadataHelper.create_from_request(payload)
    original_request = metadata["original_request"]

    assert isinstance(original_request, dict)
    assert "parse_track" not in original_request


def test_v1_rejects_null_deprecated_parse_track() -> None:
    from app.services.document_ingestion.service import (
        _validate_public_mode_selector_fields,
    )

    payload = _build_job_create(
        source_type="file",
        file_name="policy.pdf",
        parse_track=None,
    )

    with pytest.raises(ValidationException) as exc_info:
        _validate_public_mode_selector_fields(payload, api_version="v1")

    assert "API version" in exc_info.value.user_message


def test_v1_rejects_deprecated_page_memory_parse_track() -> None:
    from app.services.document_ingestion.service import (
        _validate_public_mode_selector_fields,
    )

    payload = _build_job_create(
        source_type="file",
        file_name="policy.pdf",
        parse_track="page_memory",
    )

    with pytest.raises(ValidationException) as exc_info:
        _validate_public_mode_selector_fields(payload, api_version="v1")

    assert "Use /v2/jobs" in exc_info.value.user_message


def test_v2_rejects_public_parse_track_selector() -> None:
    from app.services.document_ingestion.service import (
        _validate_public_mode_selector_fields,
    )

    payload = _build_job_create_v2(
        source_type="file",
        file_name="policy.pdf",
        parse_track="chunk",
    )

    with pytest.raises(ValidationException) as exc_info:
        _validate_public_mode_selector_fields(payload, api_version="v2")

    assert "API version" in exc_info.value.user_message


def test_public_job_create_rejects_unknown_top_level_fields() -> None:
    from app.services.document_ingestion.service import (
        _validate_public_mode_selector_fields,
    )

    payload = _build_job_create(
        source_type="file",
        file_name="policy.pdf",
        processing_generation="page_memory",
    )

    with pytest.raises(ValidationException) as exc_info:
        _validate_public_mode_selector_fields(payload, api_version="v1")

    assert "Unsupported job-create fields" in exc_info.value.user_message
    assert exc_info.value.details["violations"] == [
        {
            "field": "processing_generation",
            "description": "Remove this unsupported top-level field",
        }
    ]


def test_public_job_create_schemas_do_not_advertise_parse_track() -> None:
    v1_schema = JobCreate.model_json_schema()
    v2_schema = JobCreateV2.model_json_schema()

    assert "parse_track" not in v1_schema["properties"]
    assert "parse_track" not in v2_schema["properties"]
    assert v1_schema["additionalProperties"] is False
    assert v2_schema["additionalProperties"] is False


def test_v2_job_polling_system_rule_matches_before_default() -> None:
    from app.services.rate_limit.data_structures import SystemLimitRule
    from app.services.rate_limit.system_limit import find_system_rule

    rules = [
        SystemLimitRule(
            method="GET",
            api_pattern="/v2/jobs/*",
            priority=200,
            limit=200,
        ),
        SystemLimitRule(
            method="*",
            api_pattern="*",
            priority=9999,
            limit=1000,
        ),
    ]

    rule = find_system_rule("GET", "/v2/jobs/job_123", rules)

    assert rule.api_pattern == "/v2/jobs/*"
    assert rule.limit == 200


def test_v2_jobs_documents_and_retrieval_routes_are_registered_in_openapi() -> None:
    from main import app

    paths = set(app.openapi()["paths"])

    assert any(path.endswith("/v2/jobs") for path in paths)
    assert any(path.endswith("/v2/jobs/{job_id}") for path in paths)
    assert any(path.endswith("/v2/jobs/{job_id}/confirm-upload") for path in paths)
    assert any(path.endswith("/v2/documents") for path in paths)
    assert any(path.endswith("/v2/retrieval/query") for path in paths)


@pytest.mark.parametrize(
    "path",
    [
        "/v2/jobs",
        "/v2/jobs/job_123",
        "/v2/documents",
        "/v2/documents/doc_123",
        "/v2/retrieval/query",
    ],
)
def test_guest_route_policy_allows_v2_jobs_documents_and_retrieval(path: str) -> None:
    from app.services.rate_limit.data_structures import RouteAdmissionContext
    from app.services.rate_limit.job_admission_route_policy_service import (
        JobAdmissionRoutePolicyService,
    )

    JobAdmissionRoutePolicyService().enforce_guest_api_key_scope(
        route_context=RouteAdmissionContext(
            method="GET",
            path=path,
            limit_identifier=path,
        ),
        user_tier="guest",
    )
