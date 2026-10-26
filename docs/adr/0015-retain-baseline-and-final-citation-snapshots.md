# Retain baseline and final citation snapshots

## Status

Accepted during the progressive-ingestion planning discussion on 2026-10-26.

## Context

A progressive Document can receive citations while its baseline is the visible
representation. Full enrichment can change content, chunk boundaries, and page
assets. Replacing that representation in place would change the evidence behind
existing citations.

## Decision

Retain the published baseline and final enriched snapshot, including the
resources needed to read each. After the final snapshot has synchronized and
passed its commit gate, switch the default reader to it atomically. Existing
citations remain pinned to their original snapshot; do not remap them silently
to the final content.

Only the two reader publication milestones require retained snapshots. This
decision does not require publishing or retaining each intermediate scope
refinement. Existing document deletion and retention rules still apply.

When a citation opens a snapshot older than the currently readable result, show
`Viewing the cited version` with an explicit `View latest version` action. Keep
the cited snapshot visible by default. Choosing the latest version leaves the
pinned reader context; it does not rewrite the saved citation or move its
highlight onto different evidence.

## Consequences

Storage must accommodate both published representations. Snapshot and archive
identities must distinguish the baseline from the final result within the same
Job, and preserve the content and assets referenced by old citations. Replacing
an archive at the same key or weakening SDK archive-identity checks is
insufficient. Exact internal identity and addressing changes remain technical
design work; this decision introduces no new ingestion request parameter.
