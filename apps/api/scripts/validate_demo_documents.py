from __future__ import annotations
# ruff: noqa: E402

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _bootstrap_python_path() -> None:
    current_dir = Path(__file__).resolve().parent
    api_root = current_dir.parent
    repo_root = api_root.parent.parent
    shared_python_path = repo_root / "packages" / "shared-python"

    for path in (api_root, shared_python_path):
        path_value = os.fspath(path)
        if path_value not in sys.path:
            sys.path.insert(0, path_value)


_bootstrap_python_path()

from app.services.demo.source_catalog import DemoSourceCatalog, DemoSourceDefinition
from shared.services.storage.zip_result_schema import ZipResultSchemaBuilder


@dataclass(frozen=True)
class DemoValidationIssue:
    source_id: str
    message: str


def validate_demo_documents(*, write: bool = False) -> list[DemoValidationIssue]:
    catalog = DemoSourceCatalog()
    issues: list[DemoValidationIssue] = []
    for source in catalog.list_sources():
        issues.extend(_validate_source(catalog=catalog, source=source, write=write))
    return issues


def _validate_source(
    *,
    catalog: DemoSourceCatalog,
    source: DemoSourceDefinition,
    write: bool,
) -> list[DemoValidationIssue]:
    issues: list[DemoValidationIssue] = []
    source_directory = catalog.source_directory(source)
    chunks = _load_chunks(source_directory)
    if len(chunks) != source.chunk_count:
        issues.append(
            DemoValidationIssue(
                source_id=source.demo_source_id,
                message=(
                    f"chunk_count mismatch: catalog={source.chunk_count}, "
                    f"chunks.json={len(chunks)}"
                ),
            )
        )

    original_path = source_directory / "original.pdf"
    if not original_path.is_file():
        issues.append(
            DemoValidationIssue(
                source_id=source.demo_source_id,
                message="original.pdf is missing",
            )
        )
    elif original_path.stat().st_size != source.size_bytes:
        issues.append(
            DemoValidationIssue(
                source_id=source.demo_source_id,
                message=(
                    f"size_bytes mismatch: catalog={source.size_bytes}, "
                    f"original.pdf={original_path.stat().st_size}"
                ),
            )
        )

    _validate_catalog_projection(catalog=catalog, source=source, issues=issues)
    _validate_doc_nav(
        source=source,
        source_directory=source_directory,
        chunks=chunks,
        write=write,
        issues=issues,
    )
    return issues


def _load_chunks(source_directory: Path) -> list[dict[str, Any]]:
    chunks_path = source_directory / "chunks.json"
    payload = json.loads(chunks_path.read_text(encoding="utf-8"))
    raw_chunks = payload.get("chunks") if isinstance(payload, dict) else None
    if not isinstance(raw_chunks, list):
        return []
    return [chunk for chunk in raw_chunks if isinstance(chunk, dict)]


def _validate_catalog_projection(
    *,
    catalog: DemoSourceCatalog,
    source: DemoSourceDefinition,
    issues: list[DemoValidationIssue],
) -> None:
    try:
        catalog.get_catalog()
    except Exception as exc:
        issues.append(
            DemoValidationIssue(
                source_id=source.demo_source_id,
                message=f"catalog projection failed: {exc}",
            )
        )


def _validate_doc_nav(
    *,
    source: DemoSourceDefinition,
    source_directory: Path,
    chunks: list[dict[str, Any]],
    write: bool,
    issues: list[DemoValidationIssue],
) -> None:
    doc_nav_path = source_directory / "doc_nav.json"
    expected_doc_nav = ZipResultSchemaBuilder().build_doc_nav(chunks, source.title)
    expected_text = _serialize_json(expected_doc_nav)
    if write:
        doc_nav_path.write_text(expected_text, encoding="utf-8")
        return

    if not doc_nav_path.is_file():
        issues.append(
            DemoValidationIssue(
                source_id=source.demo_source_id,
                message="doc_nav.json is missing; run with --write",
            )
        )
        return

    current_text = doc_nav_path.read_text(encoding="utf-8")
    if current_text != expected_text:
        issues.append(
            DemoValidationIssue(
                source_id=source.demo_source_id,
                message="doc_nav.json is stale; run with --write",
            )
        )


def _serialize_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and optionally regenerate canonical demo documents.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Regenerate doc_nav.json files from chunks.json before validating.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    issues = validate_demo_documents(write=bool(args.write))
    if issues:
        for issue in issues:
            print(f"[FAIL] {issue.source_id}: {issue.message}")
        return 1

    action = "regenerated and validated" if args.write else "validated"
    print(f"Demo documents {action}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
