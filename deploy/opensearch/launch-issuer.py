"""Launch an approved operator in one bounded, fenceable systemd system unit."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4


_ROOT: Path = Path(__file__).resolve().parents[2]
_PROGRAMS: dict[str, Path] = {"operator": _ROOT / "deploy/opensearch/operator.py",
    "recovery": _ROOT / "deploy/opensearch/recovery.py",
    "release-step": _ROOT / "deploy/opensearch/release.py",
    "workload": _ROOT / "scripts/probes/run-opensearch-workload.py"}


def _buildCommand(kind: str, arguments: list[str], *, token: str, maximumSeconds: int,
        environmentFile: Path, python: Path) -> list[str]:
    if (not 1 <= maximumSeconds <= 14_400 or not python.is_absolute() or not environmentFile.is_absolute()
            or "--execute" in arguments or "--issuer-token" in arguments
            or any(char in str(environmentFile) for char in ("%", "\n", "\r", "\x00"))):
        raise ValueError("Bind one runtime, private environment file and bounded physical invocation")
    unit: str = "knowhere-native-issuer-" + UUID(token).hex + ".service"
    return ["/usr/bin/systemd-run", "--system", "--wait", "--collect", "--quiet", "--service-type=exec",
        "--expand-environment=no", "--unit=" + unit, "--working-directory=" + str(_ROOT),
        "--property=User=knowhere-ops", "--property=KillMode=control-group", "--property=UMask=0077",
        "--property=NoNewPrivileges=true", "--property=TimeoutStopSec=15s",
        "--property=RuntimeMaxSec=" + str(maximumSeconds) + "s", "--property=EnvironmentFile=" + str(environmentFile),
        str(python), str(_PROGRAMS[kind]), *arguments, "--issuer-token", token, "--execute"]


def _main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=tuple(_PROGRAMS), required=True)
    parser.add_argument("--environment-file", required=True, type=Path)
    parser.add_argument("--maximum-seconds", required=True, type=int)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--token", help="Predeclared one-use physical UUID, required by a frozen workload")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args: argparse.Namespace = parser.parse_args()
    if args.kind == "workload" and not args.token:
        raise ValueError("A frozen workload binds its approved one-use issuer UUID")
    token: str = str(UUID(args.token)) if args.token else str(uuid4())
    child: list[str] = list(args.arguments)
    if child and child[0] == "--":
        child.pop(0)
    command: list[str] = _buildCommand(args.kind, child, token=token, maximumSeconds=args.maximum_seconds,
        environmentFile=args.environment_file, python=args.python)
    if not args.execute:
        print(json.dumps({"mode": "dry-run", "command": command, "invocation_token": token,
            "resource_mutations_issued": False}))
        return 0
    if os.geteuid() != 0 or not args.environment_file.is_file() or args.environment_file.stat().st_mode & 0o077:
        raise ValueError("Launch only on the reviewed system host with a private root-owned environment file")
    if args.environment_file.stat().st_uid != 0:
        raise ValueError("System service environment files must be root-owned")
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (ValueError, OSError) as error:
        print("Operator issuer was not launched: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(2) from None
