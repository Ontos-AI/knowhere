"""Explicit source-owned staging workload callback; dry-run by default."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

_ROOT: Path = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "packages/shared-python"))
sys.path.insert(0, str(_ROOT / "apps/api"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", required=True, type=Path)
    parser.add_argument("--expected-configuration-sha256")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    from shared.core.database_sync import get_sync_session_factory
    from shared.services.retrieval.lexical.workload_probe import WorkloadSourceAdapter, _readPrivate
    if arguments.execute and not arguments.expected_configuration_sha256:
        raise ValueError("Execution requires the reviewed source-adapter configuration SHA-256")
    configuration = _readPrivate(str(arguments.configuration), 8 * 1024 * 1024,
        expectedSha256=arguments.expected_configuration_sha256)
    request = _readPrivate(str(arguments.request), 1024 * 1024)
    if arguments.receipt.resolve() in (arguments.configuration.resolve(), arguments.request.resolve()):
        raise ValueError("Receipt cannot overwrite a campaign input")
    descriptor: int = os.open(arguments.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        adapter = WorkloadSourceAdapter(configuration=configuration, sessionFactory=get_sync_session_factory())
        try:
            receipt = asyncio.run(adapter.runAsync(request, execute=arguments.execute))
        except Exception as error:
            receipt = {"mode": "actual" if arguments.execute else "dry-run", "status": "failed",
                "campaign_id": configuration.get("campaign_id"), "source_hash": configuration.get("source_hash"),
                "errors": [type(error).__name__]}
        output.write(json.dumps(receipt, sort_keys=True) + "\n")
    print(json.dumps(receipt, sort_keys=True))
    return 1 if receipt.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
