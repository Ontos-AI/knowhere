"""Freeze the production-shaped publication input.

Reads the published chunks of one production-shaped revision from the read-only
source database and writes the ignored frozen payload plus its manifest. The
command performs SELECTs only; it never writes to the source database.

    uv run python scripts/publication_benchmark/freeze_input.py \
      --source-db-url-file /tmp/knowhere-prod-db-url-read-only \
      --source-revision <parse-input-revision> \
      --output .benchmarks/publication/inputs/spacex-s1-production
"""

from __future__ import annotations

import argparse
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.frozen_input import (  # noqa: E402
    FROZEN_INPUT_SCHEMA_VERSION,
    PUBLICATION_INPUT_SCHEMA_VERSION,
    SPACEX_S1_PRODUCTION_EXPECTATIONS,
    FrozenInputCounts,
    FrozenInputError,
    FrozenInputManifest,
    canonical_chunk_sort_key,
    compute_payload_digest,
    validate_payload,
    write_frozen_input,
)
from scripts.publication_benchmark.layout import (  # noqa: E402
    FROZEN_MANIFEST_FILE_NAME,
    FROZEN_PAYLOAD_FILE_NAME,
)

FREEZE_RESULT_FILE_NAME: str = "freeze-result.json"


def load_source_database_url(path: Path) -> str:
    """Read the read-only source database URL."""
    if not path.is_file():
        raise FrozenInputError(f"source database URL file not found: {path}")
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(lines) != 1:
        raise FrozenInputError(
            "source database URL file must contain exactly one line"
        )
    return lines[0]


def build_payload_chunks(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build canonical publication-input chunks from published chunk rows."""
    chunks: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        raw_metadata = row.get("chunk_metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        raw_order = row.get("sort_order")
        order = index if raw_order is None else int(raw_order)
        chunks.append(
            {
                "chunk_id": str(row.get("chunk_id") or ""),
                "type": str(row.get("chunk_type") or "text"),
                "content": row.get("content"),
                "order": order,
                "path": row.get("source_chunk_path"),
                "file_path": row.get("file_path"),
                "metadata": metadata,
            }
        )
    chunks.sort(key=canonical_chunk_sort_key)
    return chunks


def read_revision_chunks(
    database_url: str,
    *,
    source_revision: str,
) -> tuple[list[dict[str, Any]], str]:
    """Read the published publication chunks of one source revision."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    engine = create_engine(database_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            revision_row = (
                connection.execute(
                    text(
                        "SELECT id, job_id, document_id FROM job_results "
                        "WHERE id = :revision"
                    ),
                    {"revision": source_revision},
                )
                .mappings()
                .first()
            )
            if revision_row is None:
                raise FrozenInputError(
                    f"source revision {source_revision!r} does not exist in the "
                    "source database"
                )
            document_id = (
                None
                if revision_row["document_id"] is None
                else str(revision_row["document_id"])
            )
            if document_id is None:
                raise FrozenInputError(
                    f"source revision {source_revision!r} is not bound to a "
                    "published document"
                )
            rows = list(
                connection.execute(
                    text(
                        "SELECT chunk_id, chunk_type, content, source_chunk_path, "
                        "file_path, chunk_metadata, sort_order FROM document_chunks "
                        "WHERE document_id = :document_id AND "
                        "job_result_id = :revision "
                        "ORDER BY sort_order, chunk_id, id"
                    ),
                    {
                        "revision": source_revision,
                        "document_id": document_id,
                    },
                ).mappings()
            )
    finally:
        engine.dispose()

    normalized_rows = [cast(Mapping[str, Any], row) for row in rows]
    return build_payload_chunks(normalized_rows), document_id


def read_source_counts(
    database_url: str,
    *,
    source_revision: str,
    document_id: str,
) -> tuple[int, int, str]:
    """Read map-unit, token-row, and opaque database identity counts."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    engine = create_engine(database_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            scope_parameters = {
                "revision": source_revision,
                "document_id": document_id,
            }
            map_units = int(
                connection.execute(
                    text(
                        "SELECT count(*) FROM document_map_units WHERE "
                        "document_id = :document_id AND "
                        "job_result_id = :revision"
                    ),
                    scope_parameters,
                ).scalar()
                or 0
            )
            token_rows = int(
                connection.execute(
                    text(
                        "SELECT count(*) FROM document_map_unit_tokens WHERE "
                        "map_unit_id IN (SELECT id FROM document_map_units "
                        "WHERE document_id = :document_id AND "
                        "job_result_id = :revision)"
                    ),
                    scope_parameters,
                ).scalar()
                or 0
            )
            identity_row = (
                connection.execute(
                    text(
                        "SELECT current_database() AS database_name, "
                        "inet_server_addr()::text AS host, "
                        "inet_server_port() AS port, "
                        "(SELECT system_identifier FROM pg_control_system())::text "
                        "AS system_identifier"
                    )
                )
                .mappings()
                .first()
            )
    finally:
        engine.dispose()

    identity = dict(cast(Mapping[str, Any], identity_row or {}))
    identity_raw = "|".join(
        str(identity.get(field) or "")
        for field in ("database_name", "host", "port", "system_identifier")
    )
    identity_digest = "db-" + sha256(identity_raw.encode("utf-8")).hexdigest()[:20]
    return map_units, token_rows, identity_digest


def build_manifest(
    *,
    payload: Mapping[str, Any],
    counts: FrozenInputCounts,
    source_revision: str,
    source_database_identity: str,
    generated_at: str,
) -> FrozenInputManifest:
    """Build the frozen input manifest for a validated payload."""
    from scripts.publication_benchmark.frozen_input import (
        canonical_payload_bytes,
    )

    digest = compute_payload_digest(payload)
    canonical_bytes = canonical_payload_bytes(payload)
    import gzip

    compressed = gzip.compress(canonical_bytes, compresslevel=6, mtime=0)
    corpus_fingerprint = "corpus-" + sha256(
        f"{source_revision}|{digest}|{counts.to_dict()}".encode("utf-8")
    ).hexdigest()[:24]
    return FrozenInputManifest(
        schema_version=FROZEN_INPUT_SCHEMA_VERSION,
        payload_schema_version=PUBLICATION_INPUT_SCHEMA_VERSION,
        generated_at=generated_at,
        source_revision=source_revision,
        corpus_fingerprint=corpus_fingerprint,
        digest=digest,
        payload_file=FROZEN_PAYLOAD_FILE_NAME,
        payload_bytes=len(canonical_bytes),
        payload_gzip_bytes=len(compressed),
        counts=counts,
        source_database_identity=source_database_identity,
    )


def freeze_input(
    *,
    source_database_url: str,
    source_revision: str,
    output_directory: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Freeze one publication input and return its machine-readable result."""
    payload_chunks, document_id = read_revision_chunks(
        source_database_url,
        source_revision=source_revision,
    )
    payload: dict[str, Any] = {
        "schema_version": PUBLICATION_INPUT_SCHEMA_VERSION,
        "chunks": payload_chunks,
    }
    payload_counts = validate_payload(payload)
    map_units, token_rows, identity_digest = read_source_counts(
        source_database_url,
        source_revision=source_revision,
        document_id=document_id,
    )
    counts = FrozenInputCounts(
        chunks=payload_counts.chunks,
        text_chunks=payload_counts.text_chunks,
        image_chunks=payload_counts.image_chunks,
        table_chunks=payload_counts.table_chunks,
        chunks_with_metadata=payload_counts.chunks_with_metadata,
        chunks_with_artifact_path=payload_counts.chunks_with_artifact_path,
        map_units=map_units,
        token_rows=token_rows,
    )
    manifest = build_manifest(
        payload=payload,
        counts=counts,
        source_revision=source_revision,
        source_database_identity=identity_digest,
        generated_at=generated_at or cli_support.utc_now_iso(),
    )
    payload_counts = validate_payload(payload, recorded_counts=counts)
    write_frozen_input(output_directory, payload=payload, manifest=manifest)
    result: dict[str, Any] = {
        "command": "freeze_input",
        "status": "ok",
        "output_directory": str(output_directory),
        "manifest_file": FROZEN_MANIFEST_FILE_NAME,
        "payload_file": FROZEN_PAYLOAD_FILE_NAME,
        "digest": manifest.digest,
        "corpus_fingerprint": manifest.corpus_fingerprint,
        "counts": payload_counts.to_dict(),
        "expectations": {
            "chunks": SPACEX_S1_PRODUCTION_EXPECTATIONS.chunks,
            "map_units": SPACEX_S1_PRODUCTION_EXPECTATIONS.map_units,
            "token_rows": SPACEX_S1_PRODUCTION_EXPECTATIONS.token_rows,
        },
    }
    cli_support.write_json(output_directory / FREEZE_RESULT_FILE_NAME, result)
    return result


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the freeze_input command line parser."""
    parser = argparse.ArgumentParser(
        description="Freeze the production-shaped publication input",
    )
    parser.add_argument(
        "--source-db-url-file",
        required=True,
        type=Path,
        help="File containing the read-only source database URL",
    )
    parser.add_argument(
        "--source-revision",
        required=True,
        help="Job-result identifier of the parsed source revision",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Frozen input directory",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the freeze_input command."""
    arguments = build_argument_parser().parse_args(argv)
    try:
        source_database_url = load_source_database_url(arguments.source_db_url_file)
        result = freeze_input(
            source_database_url=source_database_url,
            source_revision=arguments.source_revision,
            output_directory=arguments.output,
        )
    except FrozenInputError as error:
        cli_support.print_result(
            {"command": "freeze_input", "status": "failed", "error": str(error)}
        )
        return cli_support.EXIT_GATE_FAILED
    cli_support.print_result(result)
    return cli_support.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
