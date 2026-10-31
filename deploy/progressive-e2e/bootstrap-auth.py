"""Write a fresh isolated Knowhere API credential without printing its value."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from scripts.init_user import initialize_standalone_user


async def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments: argparse.Namespace = parser.parse_args()
    output: Path = arguments.output
    if output.exists():
        raise FileExistsError(
            f"Refusing to replace an existing test credential: {output}"
        )
    initialized: dict[str, str] = dict(
        await initialize_standalone_user(
            email="progressive-e2e@knowhere.test",
            user_id="progressive-e2e-owner",
            name="Progressive E2E",
            key_name="progressive-e2e",
            tier="tier_5",
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(initialized, stream)
        stream.write("\n")
    print(f"Created isolated test identity and wrote credential to {output}")


if __name__ == "__main__":
    asyncio.run(main())
