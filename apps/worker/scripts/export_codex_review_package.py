"""Build a standalone local MinerU-backed Codex review package."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

WORKER_ROOT = Path(__file__).resolve().parents[1]
if str(WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_ROOT))

from app.services.codex_export.package_builder import (  # noqa: E402
    ReviewPackageError,
    ReviewPackageRequest,
    build_codex_review_package,
)
from app.services.document_parser.providers.mineru.artifact_contract import (  # noqa: E402
    MinerUArtifactContractError,
)
from app.services.document_parser.providers.mineru.local_process import (  # noqa: E402
    LocalMinerUError,
)


def _parse_pages(value: str) -> tuple[int, ...]:
    if not value.strip():
        return ()
    try:
        pages = tuple(sorted({int(part.strip()) for part in value.split(",")}))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "pages must be a comma-separated list of integers"
        ) from error
    if any(page < 1 for page in pages):
        raise argparse.ArgumentTypeError("pages must use positive one-based numbers")
    return pages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a portable local Codex document review package."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mineru-project", type=Path, required=True)
    parser.add_argument("--lang", required=True)
    parser.add_argument("--backend", default="pipeline")
    parser.add_argument("--method", default="auto")
    parser.add_argument("--pages", type=_parse_pages, default=())
    parser.add_argument(
        "--include-table-pages",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--include-image-pages",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument(
        "--offline",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--keep-work-dir", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request = ReviewPackageRequest(
        source_path=args.input,
        output_root=args.output,
        mineru_project_path=args.mineru_project,
        backend=args.backend,
        method=args.method,
        language=args.lang,
        requested_pages=args.pages,
        include_table_pages=args.include_table_pages,
        include_image_pages=args.include_image_pages,
        dpi=args.dpi,
        offline=args.offline,
        force=args.force,
        keep_work_dir=args.keep_work_dir,
    )
    try:
        result = build_codex_review_package(request)
    except (
        LocalMinerUError,
        MinerUArtifactContractError,
        ReviewPackageError,
        ValueError,
    ) as error:
        print(f"codex-review-export: {error}", file=sys.stderr)
        return 2
    print(result.package_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
