"""Inspect or collect retained indexing inputs as an on-demand operation."""

import argparse
import json
import sys


def run() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    modes: argparse._MutuallyExclusiveGroup = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="Count eligible inputs (default)")
    modes.add_argument("--apply", action="store_true", help="Recheck and delete eligible inputs")
    modes.add_argument("--discover", action="store_true", help="Refresh the S3 input inventory")
    options: argparse.Namespace = parser.parse_args()

    try:
        from shared.services.retrieval.lexical.input_collector import LexicalInputCollector
        from shared.services.storage.object_inventory_page import ObjectInventoryPage

        collector: LexicalInputCollector = LexicalInputCollector()
        if options.discover:
            listedObjects: int = 0
            while True:
                page: ObjectInventoryPage | None = collector.discover(isImmediate=True)
                if page is None:
                    raise RuntimeError("Input inventory cursor moved during discovery")
                listedObjects += len(page.objects)
                if page.nextCursor is None:
                    break
            print(json.dumps({"mode": "discover", "listed_objects": listedObjects}))
            return 0

        assigned: tuple[str, ...]
        orphans: tuple[str, ...]
        assigned, orphans = collector.findCandidates()
        candidates: int = len(assigned) + len(orphans)
        report: dict[str, int | str] = {
            "mode": "apply" if options.apply else "dry-run",
            "candidates": candidates,
            "assigned_candidates": len(assigned),
            "orphan_candidates": len(orphans),
        }
        if not options.apply:
            print(json.dumps(report))
            return 0

        collected: int = sum(collector.collectAssigned(revision) for revision in assigned)
        collected += sum(collector.collectOrphan(key) for key in orphans)
        report.update(collected=collected, not_collected=candidates - collected)
        print(json.dumps(report))
        return int(collected != candidates)
    except Exception as error:
        print(f"Indexing input maintenance failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
