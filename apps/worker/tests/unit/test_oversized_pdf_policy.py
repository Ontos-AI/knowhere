from __future__ import annotations

import json

import pytest

from app.services.document_parser.orchestration import oversized_pdf_policy as policy
from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    PDFParsingException,
    ValidationException,
)
from shared.core.exceptions.knowhere_exception import KnowhereException

_REQUEST_ID = "req-oversized-pdf-policy"


def _configure_pdf_limits(
    monkeypatch: pytest.MonkeyPatch,
    *,
    page_limit: int = 2,
    shard_enabled: bool = True,
    soft_limit: int = 10,
) -> None:
    monkeypatch.setattr(settings, "MAX_PDF_PAGE_LIMIT", page_limit)
    monkeypatch.setattr(settings, "OVERSIZED_PDF_SHARD_ENABLED", shard_enabled)
    monkeypatch.setattr(settings, "OVERSIZED_PDF_SOFT_LIMIT", soft_limit)


def _client_payload(exc: KnowhereException) -> dict[str, object]:
    return exc.to_client(_REQUEST_ID)


def _assert_client_payload_omits_internal_message(exc: KnowhereException) -> None:
    payload = _client_payload(exc)
    dumped = json.dumps(payload)
    assert "internal_message" not in dumped
    assert payload["error"]["message"] == exc.user_message
    if exc.internal_message != exc.user_message:
        assert exc.internal_message not in dumped


def test_non_pdf_files_are_not_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_pdf_limits(monkeypatch, page_limit=1, shard_enabled=False, soft_limit=2)

    assert (
        policy.build_oversized_pdf_rejection(
            file_extension=".docx",
            page_count=999,
        )
        is None
    )
    assert (
        policy.build_oversized_pdf_rejection(
            file_extension=".png",
            page_count=999,
        )
        is None
    )


def test_pdf_at_or_below_direct_page_limit_is_not_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_pdf_limits(monkeypatch, page_limit=2, shard_enabled=False, soft_limit=10)

    assert (
        policy.build_oversized_pdf_rejection(
            file_extension=".pdf",
            page_count=2,
        )
        is None
    )
    assert (
        policy.build_oversized_pdf_rejection(
            file_extension=".PDF",
            page_count=1,
        )
        is None
    )
    policy.raise_if_oversized_pdf_not_supported(page_count=2)


def test_sharding_disabled_rejects_pages_above_direct_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_pdf_limits(monkeypatch, page_limit=2, shard_enabled=False, soft_limit=10)

    rejection = policy.build_oversized_pdf_rejection(
        file_extension=".pdf",
        page_count=3,
    )

    assert isinstance(rejection, ValidationException)
    assert rejection.user_message == (
        "Document too large: 3 pages exceeds the 2-page "
        "limit. Please split the document and upload it in smaller parts."
    )
    assert rejection.details == {
        "violations": [
            {
                "field": "page_count",
                "description": "PDF has 3 pages, limit is 2",
            }
        ]
    }
    _assert_client_payload_omits_internal_message(rejection)

    with pytest.raises(ValidationException) as exc_info:
        policy.raise_if_oversized_pdf_not_supported(page_count=3)
    assert exc_info.value.user_message == rejection.user_message
    assert exc_info.value.details == rejection.details


def test_sharding_enabled_admits_pages_at_soft_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_pdf_limits(monkeypatch, page_limit=2, shard_enabled=True, soft_limit=10)

    assert (
        policy.build_oversized_pdf_rejection(
            file_extension=".pdf",
            page_count=10,
        )
        is None
    )
    policy.raise_if_oversized_pdf_not_supported(page_count=3)


def test_pages_above_soft_limit_use_contact_support_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_pdf_limits(monkeypatch, page_limit=2, shard_enabled=True, soft_limit=10)

    rejection = policy.build_oversized_pdf_rejection(
        file_extension=".pdf",
        page_count=11,
    )

    assert isinstance(rejection, ValidationException)
    assert rejection.user_message == (
        "This document has 11 pages. Processing ultra-long documents "
        "over 10 pages requires dedicated resources. Please contact "
        "support for assistance."
    )
    assert rejection.details == {
        "violations": [
            {
                "field": "page_count",
                "description": "PDF has 11 pages, soft limit is 10",
            }
        ]
    }
    payload = _client_payload(rejection)
    assert payload["error"]["message"] == rejection.user_message
    assert payload["error"]["details"] == rejection.details
    _assert_client_payload_omits_internal_message(rejection)


def test_shard_failure_exposes_user_message_and_reason_not_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_pdf_limits(monkeypatch, page_limit=2)

    original = RuntimeError("MinerU shard 0 failed")
    exc = policy.build_oversized_pdf_processing_failed_exception(
        page_count=5,
        original_exception=original,
    )

    assert isinstance(exc, PDFParsingException)
    assert exc.details == {
        "file_type": "pdf",
        "reason": "OVERSIZED_SHARD_PIPELINE_FAILED",
    }
    assert exc.user_message == (
        "Oversized PDF processing failed during sharding: "
        "RuntimeError: MinerU shard 0 failed. This document has 5 pages and exceeds the "
        "2-page direct processing limit, so it cannot be processed "
        "without a successful shard pipeline."
    )
    assert "falling back to page-limit rejection" in exc.internal_message
    payload = _client_payload(exc)
    assert payload["error"]["message"] == exc.user_message
    assert payload["error"]["details"]["reason"] == "OVERSIZED_SHARD_PIPELINE_FAILED"
    _assert_client_payload_omits_internal_message(exc)


def test_profile_failure_exposes_user_message_and_reason_not_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_pdf_limits(monkeypatch, page_limit=2)

    original = ValidationException(
        user_message="structural profile timed out",
        violations=[{"field": "profile", "description": "timeout"}],
    )
    exc = policy.build_oversized_pdf_profile_failed_exception(
        page_count=8,
        original_exception=original,
    )

    assert isinstance(exc, PDFParsingException)
    assert exc.details["reason"] == "OVERSIZED_PAGE_MEMORY_PROFILE_FAILED"
    assert exc.user_message == (
        "Oversized PDF page-memory profiling failed: "
        "structural profile timed out. This document has 8 pages and requires a "
        "successful structural profile before page-memory parsing can continue."
    )
    assert "page-memory structural profile failed" in exc.internal_message
    payload = _client_payload(exc)
    assert payload["error"]["message"] == exc.user_message
    assert payload["error"]["details"]["reason"] == (
        "OVERSIZED_PAGE_MEMORY_PROFILE_FAILED"
    )
    _assert_client_payload_omits_internal_message(exc)
