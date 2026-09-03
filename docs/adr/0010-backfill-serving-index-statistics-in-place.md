# Backfill serving-index statistics in place

**Status: accepted.** Existing retrieval-visible data will be maintained by
backfilling only the current revision of each active Document. Complete serving
indexes receive the new per-channel BM25 statistics through the statistics-only
`apps/api/scripts/backfill_map_unit_statistics.py --apply` command. It aggregates
existing map units and does not regenerate token rows, manifests, or namespace
snapshots. Missing or legacy indexes use the existing full
`apps/api/scripts/backfill_map_unit_indexes.py --apply --document-id ...`
rebuild. v1 and v2 are internal read paths under the same Retrieval contract,
so each revision commits independently. While the four statistics are NULL,
the reader keeps the full scope-first map-unit projection and derives the
existing per-channel denominators from those rows; it does not switch to a
semantically different FTS query. Once statistics are complete, the
token-selective projection may be used. Missing, legacy, or storage-
inconsistent index data still uses the exact legacy reader. An interrupted or
failed backfill therefore preserves retrieval semantics and avoids unnecessary
regeneration of correct data.
