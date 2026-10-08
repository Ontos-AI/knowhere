"""Prepare canonical demo result artifacts without materializing documents."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def _bootstrap_python_path() -> None:
    api_root: Path = Path(__file__).resolve().parents[1]
    shared_root: Path = api_root.parents[1] / "packages" / "shared-python"
    for root in (api_root, shared_root):
        value: str = os.fspath(root)
        if value not in sys.path:
            sys.path.insert(0, value)


_bootstrap_python_path()

from app.services.demo.canonical_bundle import CanonicalDemoBundleStore  # noqa: E402
from app.services.demo.source_catalog import (  # noqa: E402
    DemoSourceCatalog,
    DemoSourceDefinition,
)
from shared.services.redis.redis_service import RedisService  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_ids", metavar="SOURCE_ID", nargs="*")
    parser.add_argument(
        "--all", action="store_true", help="Select every catalog demo source."
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload missing canonical bundles to the configured result bucket.",
    )
    return parser


def _select_sources(
    parser: argparse.ArgumentParser,
    catalog: DemoSourceCatalog,
    *,
    source_ids: list[str],
    all_sources: bool,
) -> list[DemoSourceDefinition]:
    if all_sources and source_ids:
        parser.error("--all cannot be combined with source IDs")
    if not all_sources and not source_ids:
        parser.error("provide source IDs or --all")
    if all_sources:
        return list(catalog.list_sources())

    sources: list[DemoSourceDefinition] = []
    for source_id in dict.fromkeys(source_ids):
        source: DemoSourceDefinition | None = catalog.get_source(source_id)
        if source is None:
            parser.error(f"unknown demo source ID: {source_id}")
        sources.append(source)
    return sources


async def _upload_sources(
    *, catalog: DemoSourceCatalog, sources: list[DemoSourceDefinition]
) -> None:
    redis_service: RedisService = RedisService()
    try:
        store: CanonicalDemoBundleStore = CanonicalDemoBundleStore(
            redis_service=redis_service
        )
        for source in sources:
            bundle = await store.ensure_bundle(
                source_id=source.demo_source_id,
                source_directory=catalog.source_directory(source),
            )
            state: str = "reused" if bundle.reused else "created"
            print(
                f"{state} {source.demo_source_id} "
                f"version={bundle.content_version} "
                f"zip={bundle.zip_key} raw_prefix={bundle.raw_prefix}"
            )
    finally:
        await redis_service.close()


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = _build_parser()
    arguments: argparse.Namespace = parser.parse_args(argv)
    catalog: DemoSourceCatalog = DemoSourceCatalog()
    sources: list[DemoSourceDefinition] = _select_sources(
        parser,
        catalog,
        source_ids=arguments.source_ids,
        all_sources=arguments.all,
    )
    if not arguments.upload:
        for source in sources:
            print(f"dry-run {source.demo_source_id} {source.title}")
        print(f"Selected {len(sources)} source(s); pass --upload to prepare bundles.")
        return 0

    try:
        asyncio.run(_upload_sources(catalog=catalog, sources=sources))
    except Exception as error:
        print(f"Canonical demo preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
