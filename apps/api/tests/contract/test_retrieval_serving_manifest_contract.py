"""Contract tests for serving-manifest integrity and version handling."""

from __future__ import annotations

import pytest

from shared.services.retrieval.serving_manifest import (
    SERVING_MANIFEST_FORMAT_VERSION,
    decode_serving_manifest,
    encode_serving_manifest,
)


def test_serving_manifest_round_trip_preserves_canonical_payload() -> None:
    payload = {
        "document_id": "doc_contract",
        "job_result_id": "result_contract",
        "sections": [{"section_id": "sec_1", "sort_order": 0}],
        "chunks": [{"chunk_id": "chunk_1", "connect_to": []}],
    }

    compressed, checksum, version = encode_serving_manifest(payload)

    assert version == SERVING_MANIFEST_FORMAT_VERSION
    assert decode_serving_manifest(
        compressed,
        checksum=checksum,
        format_version=version,
    ) == payload


def test_serving_manifest_rejects_checksum_mismatch() -> None:
    compressed, _, version = encode_serving_manifest({"document_id": "doc"})

    with pytest.raises(ValueError, match="checksum mismatch"):
        decode_serving_manifest(
            compressed,
            checksum="0" * 64,
            format_version=version,
        )


def test_serving_manifest_rejects_unknown_version() -> None:
    compressed, checksum, _ = encode_serving_manifest({"document_id": "doc"})

    with pytest.raises(ValueError, match="unsupported serving manifest version"):
        decode_serving_manifest(
            compressed,
            checksum=checksum,
            format_version=SERVING_MANIFEST_FORMAT_VERSION + 1,
        )
