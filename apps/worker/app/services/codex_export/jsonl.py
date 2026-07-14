"""Deterministic JSON Lines writers for review-package records."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Sequence

from app.services.codex_export.schema import DocumentBlock, ExtractionFinding


def write_blocks_jsonl(
    blocks: Sequence[DocumentBlock],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            for block in blocks:
                stream.write(
                    json.dumps(
                        block.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def write_findings_jsonl(
    findings: Sequence[ExtractionFinding],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as stream:
        for finding in findings:
            stream.write(
                json.dumps(
                    finding.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            stream.write("\n")
