"""Run Codex batch validation behind temporary outbound firewall rules."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Sequence

WORKER_ROOT = Path(__file__).resolve().parents[1]
if str(WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_ROOT))

from app.services.codex_export.offline_verifier import (  # noqa: E402
    OfflineVerificationError,
    OfflineVerificationRequest,
    verify_offline_validation,
)


VALIDATOR_SCRIPT = WORKER_ROOT / "scripts" / "validate_codex_export_corpus.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Externally verify an offline Codex export validation batch."
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mineru-project", type=Path, required=True)
    parser.add_argument("--uv-executable", type=Path, required=True)
    parser.add_argument("--mineru-python", type=Path, required=True)
    parser.add_argument("--attestation", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--backend", default="pipeline")
    parser.add_argument("--method", default="auto")
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument("--force", action="store_true")
    return parser


def _validator_argv(args: argparse.Namespace) -> tuple[str, ...]:
    argv = [
        str(args.uv_executable.resolve()),
        "run",
        "python",
        str(VALIDATOR_SCRIPT),
        "--corpus",
        str(args.corpus.resolve()),
        "--output",
        str(args.output.resolve()),
        "--mineru-project",
        str(args.mineru_project.resolve()),
        "--repeat",
        str(args.repeat),
        "--backend",
        args.backend,
        "--method",
        args.method,
        "--dpi",
        str(args.dpi),
        "--offline",
    ]
    if args.force:
        argv.append("--force")
    return tuple(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request = OfflineVerificationRequest(
        validator_argv=_validator_argv(args),
        uv_executable=args.uv_executable,
        mineru_python=args.mineru_python,
        report_path=args.output / "validation-report.json",
        attestation_path=args.attestation,
        rule_id=uuid.uuid4().hex,
    )
    try:
        result = verify_offline_validation(request)
    except OfflineVerificationError as error:
        print(f"codex-offline-verification: {error}", file=sys.stderr)
        return 2
    print(result.attestation_path)
    return 0 if result.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
