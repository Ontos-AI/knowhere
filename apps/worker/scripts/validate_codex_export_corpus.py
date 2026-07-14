"""Run a repository-safe batch of local Codex review package validations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

WORKER_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = WORKER_ROOT.parents[1]
if str(WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_ROOT))

from app.services.codex_export.validation_corpus import (  # noqa: E402
    load_validation_corpus,
)
from app.services.codex_export.validation_report import (  # noqa: E402
    write_validation_reports,
)
from app.services.codex_export.validation_runner import (  # noqa: E402
    ValidationOptions,
    run_validation_corpus,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a safe batch of local Codex review package exports."
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mineru-project", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--backend", default="pipeline")
    parser.add_argument("--method", default="auto")
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument(
        "--offline", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        corpus = load_validation_corpus(
            args.corpus,
            roots={"knowhere": REPOSITORY_ROOT, "mineru": args.mineru_project},
        )
        report = run_validation_corpus(
            corpus,
            ValidationOptions(
                output_root=args.output,
                mineru_project_path=args.mineru_project,
                repeat=args.repeat,
                backend=args.backend,
                method=args.method,
                dpi=args.dpi,
                offline=args.offline,
                force=args.force,
            ),
        )
        json_path, html_path = write_validation_reports(report, args.output)
    except (OSError, ValueError) as error:
        print(f"codex-batch-validation: {error}", file=sys.stderr)
        return 2

    print(json_path)
    print(html_path)
    print(" ".join(f"{key}={value}" for key, value in report.summary.items()))
    if report.summary["expectation_mismatches"]:
        return 1
    if report.summary["reproducibility_failures"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
