"""Measure exact retained tokens and current packing locally, without services.

This is not raw-input compilation, SQL hydration or whole indexing qualification.
Use a checksummed read-only native-units.private.jsonl.gz and a new private receipt.
The parent launcher supplies any operating-system memory/time enforcement; this
probe bounds reads and rejects measured traced/RSS peaks above its declared caps.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterator
import gc
import gzip
from hashlib import sha256
import json
from pathlib import Path
import resource
import sys
from time import perf_counter
import tracemalloc
from typing import cast

from shared.services.retrieval.lexical.bulk import LexicalBulk
from shared.services.retrieval.lexical.input import LexicalInput
from shared.services.retrieval.lexical.unit import LexicalUnit

_MAXIMUM_INPUT_BYTES: int = 64 * 1024 * 1024
_MAXIMUM_DECOMPRESSED_BYTES: int = 512 * 1024 * 1024
_MAXIMUM_LINE_BYTES: int = 2 * 1024 * 1024


def _readRows(path: Path, expectedNamespaceUnits: int) -> Iterator[dict[str, object]]:
    decodedBytes: int = 0
    count: int = 0
    with gzip.open(path, "rb") as stream:
        while line := stream.readline(_MAXIMUM_LINE_BYTES + 1):
            decodedBytes += len(line)
            count += 1
            if len(line) > _MAXIMUM_LINE_BYTES or decodedBytes > _MAXIMUM_DECOMPRESSED_BYTES or count > expectedNamespaceUnits:
                raise ValueError("Retained fixture exceeds its bounded line, decompressed-byte or exact record inventory")
            row: object = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("documentId"), str) or len(row["documentId"]) > 128:
                raise ValueError("Retained fixture has an invalid document identity")
            yield cast(dict[str, object], row)
    if count != expectedNamespaceUnits:
        raise ValueError("Retained fixture does not match its exact namespace record inventory")


def _getMaximumRssBytes() -> int:
    amount: int = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return amount if sys.platform == "darwin" else amount * 1024


class RetainedInputQualification:
    @staticmethod
    def run(path: Path, *, expectedSha256: str, expectedNamespaceUnits: int = 42_794,
            expectedLargestUnits: int = 1_862, expectedTokenOccurrences: int = 103_025,
            maximumTracedBytes: int = 64 * 1024 * 1024, maximumRssBytes: int = 256 * 1024 * 1024) -> dict[str, object]:
        if path.stat().st_size > _MAXIMUM_INPUT_BYTES:
            raise ValueError("Retained fixture exceeds its compressed input byte bound")
        digest = sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != expectedSha256:
            raise ValueError("Retained fixture checksum differs from its reviewed source")
        started: float = perf_counter()
        beforeRss: int = _getMaximumRssBytes()
        tracemalloc.start()
        try:
            counts: Counter[str] = Counter(str(row["documentId"]) for row in _readRows(path, expectedNamespaceUnits))
            if not counts:
                raise ValueError("Retained fixture contains no documents")
            documentId, unitCount = counts.most_common(1)[0]
            if unitCount != expectedLargestUnits:
                raise ValueError("Retained largest document does not match its exact reviewed unit count")
            selectionPeak: int = tracemalloc.get_traced_memory()[1]
            del counts
            gc.collect()
            tracemalloc.reset_peak()
            units: list[LexicalUnit] = []
            selectedBytes: int = 0
            for row in _readRows(path, expectedNamespaceUnits):
                if row["documentId"] != documentId:
                    continue
                row.pop("buildId", None)
                selectedBytes += len(json.dumps(row, ensure_ascii=False).encode())
                if selectedBytes > _MAXIMUM_INPUT_BYTES:
                    raise ValueError("Selected retained document exceeds its input byte bound")
                units.append(LexicalUnit.model_validate(row))
            tokenCount: int = sum(len(unit.pathTokens.split()) + len(unit.contentTokens.split()) for unit in units)
            if tokenCount != expectedTokenOccurrences or len({unit.revisionId for unit in units}) != 1:
                raise ValueError("Retained document token/revision inventory changed")
            # A measurement-only provenance binding; it does not assert that the
            # frozen historical tokens match this process's current compiler.
            payload: LexicalInput = LexicalInput(compilerFingerprint="retained-fixture-sha256:" + expectedSha256,
                units=tuple(units))
            serialized: bytes = payload.model_dump_json().encode()
            if len(serialized) > _MAXIMUM_INPUT_BYTES:
                raise ValueError("Retained input serialization exceeds its input byte bound")
            restored: LexicalInput = LexicalInput.model_validate_json(serialized)
            if restored != payload:
                raise ValueError("Retained token deserialization changed the original units")
            requests: int = 0
            bulkBytes: list[int] = []
            packedUnits: int = 0
            # LexicalBuild assigns canonical UUID strings. Their full length is
            # part of every action's measured UTF-8 payload, even in this probe.
            buildId: str = "00000000-0000-4000-8000-000000000001"
            for batch in LexicalBulk.createRequests(buildId=buildId, units=restored.units,
                maximumBytes=4 * 1024 * 1024):
                requests += 1
                bulkBytes.append(len(batch.textBody.encode()))
                packedUnits += batch.unitCount
            if packedUnits != expectedLargestUnits:
                raise ValueError("Bulk packing changed the retained unit inventory")
            retainedPeak: int = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        maximumRss: int = _getMaximumRssBytes()
        peak: int = max(selectionPeak, retainedPeak)
        return {"schema_version": "retained-token-memory-qualification-v1",
            "qualification": "exact-retained-token-deserialization-and-current-bulk-packing-only",
            "passed": peak <= maximumTracedBytes and maximumRss <= maximumRssBytes,
            "fixture_sha256": expectedSha256, "compressed_fixture_bytes": path.stat().st_size,
            "namespace_unit_count": expectedNamespaceUnits, "largest_unit_count": unitCount,
            "token_occurrences": tokenCount, "selected_unit_json_bytes": selectedBytes,
            "serialized_input_bytes": len(serialized), "serialized_input_sha256": sha256(serialized).hexdigest(),
            "bulk_request_count": requests, "bulk_request_bytes": bulkBytes,
            "bulk_build_id_length": len(buildId),
            "traced_selection_peak_bytes": selectionPeak, "traced_retained_peak_bytes": retainedPeak,
            "traced_peak_bytes": peak, "maximum_traced_bytes": maximumTracedBytes,
            "rss_high_water_before_bytes": beforeRss, "rss_high_water_after_bytes": maximumRss,
            "rss_high_water_delta_bytes": max(0, maximumRss - beforeRss), "maximum_rss_bytes": maximumRssBytes,
            "measured_seconds_with_tracing": perf_counter() - started,
            "raw_compilation_qualified": False, "raw_sql_hydration_budget_qualified": False,
            "compiler_compatibility_qualified": False, "whole_execution_deadline_qualified": False,
            "service_or_native_calls": 0,
            "source_sha256": {name: sha256(Path(str(sys.modules[module].__file__)).read_bytes()).hexdigest()
                for name, module in (("probe", __name__), ("bulk", LexicalBulk.__module__),
                    ("input", LexicalInput.__module__), ("unit", LexicalUnit.__module__))}}


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    arguments = parser.parse_args()
    if arguments.receipt.resolve().is_relative_to(arguments.input.parent.resolve()):
        raise ValueError("Receipt must be outside the immutable input fixture directory")
    if arguments.receipt.exists() or not arguments.receipt.parent.is_dir():
        raise ValueError("Use a new owned receipt outside the input fixture")
    result: dict[str, object] = RetainedInputQualification.run(arguments.input, expectedSha256=arguments.expected_sha256)
    with arguments.receipt.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    arguments.receipt.chmod(0o600)
    print(json.dumps(result))
    return 0 if result["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(_main())
