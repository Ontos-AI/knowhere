# Backfill serving-index statistics in place

**Status: accepted.** Existing retrieval-visible data will be maintained by
backfilling only the current revision of each active Document. Complete serving
indexes receive the new per-channel BM25 statistics through the existing
per-document `apps/api/scripts/backfill_map_unit_indexes.py --apply` rebuild;
missing or incomplete indexes use the existing full rebuild. v1 and v2 are
internal read paths under the same Retrieval contract, so each revision commits
independently and the optimized reader requires the new serving-index format
marker before it can be selected. An interrupted or failed backfill therefore
continues using the exact legacy reader, limiting database load and avoiding
unnecessary regeneration of correct token rows and snapshots.
