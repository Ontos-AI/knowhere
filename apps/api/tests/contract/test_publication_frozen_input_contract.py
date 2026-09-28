"""Contract tests for the frozen publication input payload and manifest."""

# ruff: noqa: E402

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.publication_benchmark_support import (
    build_frozen_payload,
    build_manifest,
    ensure_benchmark_import_path,
    expectations_for,
    write_synthetic_frozen_input,
)

ensure_benchmark_import_path()

from scripts.publication_benchmark.frozen_input import (  # noqa: E402
    PUBLICATION_INPUT_SCHEMA_VERSION,
    SPACEX_S1_PRODUCTION_EXPECTATIONS,
    FrozenInputError,
    compute_payload_digest,
    read_frozen_input,
    validate_payload,
)
from scripts.publication_benchmark.freeze_input import (  # noqa: E402
    build_payload_chunks,
)


def test_frozen_input_contract_matches_the_documented_spacex_counts() -> None:
    expectations = SPACEX_S1_PRODUCTION_EXPECTATIONS

    assert expectations.chunks == 922
    assert expectations.text_chunks == 670
    assert expectations.image_chunks == 99
    assert expectations.table_chunks == 153
    assert expectations.chunks_with_metadata == 922
    assert expectations.chunks_with_artifact_path == 252
    assert expectations.map_units == 830
    assert 88_000 <= expectations.token_rows <= 89_000
    low, high = expectations.token_row_bounds()
    assert low < expectations.token_rows < high


def test_payload_digest_is_stable_and_order_insensitive_to_key_order() -> None:
    payload = build_frozen_payload()
    reordered = {
        "chunks": [
            {key: chunk[key] for key in reversed(list(chunk))}
            for chunk in payload["chunks"]
        ],
        "schema_version": payload["schema_version"],
    }

    assert compute_payload_digest(payload) == compute_payload_digest(reordered)
    assert compute_payload_digest(payload).startswith("sha256:")


def test_payload_validation_accepts_a_valid_payload() -> None:
    payload = build_frozen_payload()
    counts = validate_payload(payload, expectations=expectations_for(payload))

    assert counts.chunks == len(payload["chunks"])
    assert counts.chunks_with_metadata == len(payload["chunks"])
    assert counts.chunks_with_artifact_path == 2


def test_payload_validation_rejects_a_count_mismatch() -> None:
    payload = build_frozen_payload()
    expectations = expectations_for(payload)
    payload["chunks"].pop()

    with pytest.raises(FrozenInputError, match="counts do not match"):
        validate_payload(payload, expectations=expectations)


def test_payload_validation_rejects_missing_metadata() -> None:
    payload = build_frozen_payload()
    expectations = expectations_for(payload)
    payload["chunks"][0].pop("metadata")

    with pytest.raises(FrozenInputError, match="counts do not match"):
        validate_payload(payload, expectations=expectations)


def test_payload_validation_rejects_embedded_artifact_bytes() -> None:
    payload = build_frozen_payload()
    payload["chunks"][0]["metadata"]["image_bytes"] = "QUJD"

    with pytest.raises(FrozenInputError, match="artifact file bytes"):
        validate_payload(payload, expectations=expectations_for(payload))


def test_payload_validation_rejects_non_canonical_ordering() -> None:
    payload = build_frozen_payload()
    payload["chunks"].reverse()

    with pytest.raises(FrozenInputError, match="ordering is not canonical"):
        validate_payload(payload, expectations=expectations_for(payload))


def test_manifest_validation_rejects_a_tampered_payload() -> None:
    payload = build_frozen_payload()
    manifest = build_manifest(payload)
    payload["chunks"][0]["content"] = "tampered content"

    with pytest.raises(FrozenInputError, match="digest mismatch"):
        manifest.validate_payload(payload, expectations=expectations_for(payload))


def test_manifest_validation_rejects_wrong_map_unit_or_token_counts() -> None:
    payload = build_frozen_payload()
    expectations = expectations_for(payload, map_units=830, token_rows=88_551)
    manifest = build_manifest(payload, map_units=7, token_rows=88_551)

    with pytest.raises(FrozenInputError, match="map_units"):
        validate_payload(payload, expectations=expectations, recorded_counts=manifest.counts)

    manifest = build_manifest(payload, map_units=830, token_rows=10)
    with pytest.raises(FrozenInputError, match="token_rows"):
        validate_payload(payload, expectations=expectations, recorded_counts=manifest.counts)


def test_frozen_input_round_trip_preserves_digest_and_gzip_size(
    tmp_path: Path,
) -> None:
    payload, manifest = write_synthetic_frozen_input(tmp_path)

    loaded_payload, loaded_manifest = read_frozen_input(
        tmp_path,
        expectations=expectations_for(payload),
    )

    assert loaded_manifest.digest == manifest.digest
    assert loaded_manifest.payload_gzip_bytes == (
        tmp_path / "chunks.json.gz"
    ).stat().st_size
    assert len(loaded_payload["chunks"]) == len(payload["chunks"])


def test_read_frozen_input_requires_a_manifest(tmp_path: Path) -> None:
    with pytest.raises(FrozenInputError, match="manifest not found"):
        read_frozen_input(tmp_path)


def test_freeze_input_builds_payload_from_published_chunk_rows() -> None:
    rows = [
        {
            "chunk_id": "text-chunk",
            "chunk_type": "text",
            "content": "body",
            "source_chunk_path": "source.pdf / Root / Body",
            "file_path": None,
            "chunk_metadata": {"path": "source.pdf / Root / Body", "summary": "s"},
            "sort_order": 2,
        },
        {
            "chunk_id": "image-chunk",
            "chunk_type": "image",
            "content": "[images/a.png]",
            "source_chunk_path": "images/a.png",
            "file_path": "images/a.png",
            "chunk_metadata": {
                "path": "images/a.png",
                "file_path": "images/a.png",
                "summary": "i",
            },
            "sort_order": 1,
        },
    ]

    chunks = build_payload_chunks(rows)

    assert [chunk["chunk_id"] for chunk in chunks] == ["image-chunk", "text-chunk"]
    assert chunks[0]["file_path"] == "images/a.png"
    assert chunks[1]["metadata"]["path"] == "source.pdf / Root / Body"
    payload = {
        "schema_version": PUBLICATION_INPUT_SCHEMA_VERSION,
        "chunks": chunks,
    }
    counts = validate_payload(payload, expectations=expectations_for(payload))
    assert counts.chunks == 2
    assert counts.chunks_with_artifact_path == 1
