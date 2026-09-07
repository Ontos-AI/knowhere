"""Contract tests for serving-manifest integrity and version handling."""

from __future__ import annotations

import pytest

from shared.services.retrieval.serving_manifest import (
    NAMESPACE_MAP_SNAPSHOT_FORMAT_VERSION,
    SERVING_MANIFEST_FORMAT_VERSION,
    decode_namespace_map_snapshot,
    decode_serving_manifest,
    encode_namespace_map_snapshot,
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


def test_namespace_snapshot_uses_routing_only_v2_and_reads_legacy_v1() -> None:
    payload = {
        "documents": {
            "doc_1": {
                "job_result_id": "result_1",
                "job_id": "job_1",
                "source_file_name": "private.pdf",
                "sections": [
                    {
                        "section_id": "sec_1",
                        "section_path": "Root",
                        "section_title": "Root",
                        "section_level": 0,
                        "summary": "summary",
                        "sort_order": 0,
                        "unused": "drop",
                    }
                ],
                "chunks": [
                    {
                        "chunk_id": "chunk_1",
                        "section_id": "sec_1",
                        "chunk_type": "text",
                        "sort_order": 0,
                        "connect_to": [],
                        "content": "drop",
                    }
                ],
            }
        }
    }
    compressed, checksum, version = encode_namespace_map_snapshot(payload)

    assert version == NAMESPACE_MAP_SNAPSHOT_FORMAT_VERSION
    decoded = decode_namespace_map_snapshot(
        compressed, checksum=checksum, format_version=version
    )
    document = decoded["documents"]["doc_1"]
    assert "source_file_name" not in document
    assert "unused" not in document["sections"][0]
    assert "content" not in document["chunks"][0]

    legacy_compressed, legacy_checksum, legacy_version = encode_serving_manifest(
        payload
    )
    assert decode_namespace_map_snapshot(
        legacy_compressed,
        checksum=legacy_checksum,
        format_version=legacy_version,
    ) == payload
