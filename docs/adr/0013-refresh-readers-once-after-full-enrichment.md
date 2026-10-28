# Refresh readers once after full enrichment

## Status

Accepted during the progressive-ingestion planning discussion on 2026-10-26.
The two-stage webhook contract is recorded separately in ADR 0014, ten-minute
server reconciliation in ADR 0019, and fifteen-second visible-client status checks
in ADR 0020.

## Context

Hipohipo reads a committed copy of parsed content and page assets. Refreshing
after every scope would add repeated transfers and expose readers to multiple
changes in structure and citation identity. Knowhere currently regenerates the
downloadable package after all scopes have been refined.

## Decision

Hipohipo displays the viewable baseline while full-document enrichment runs.
After the complete enriched result is available and synchronized, the reader
automatically adopts that coherent final snapshot once. Intermediate scope
refinements do not independently trigger reader snapshot replacement.

When ordinary reading adopts the final snapshot, preserve the current List/Tree
mode and source-page focus. In Tree mode, reveal the final chapter containing
that source page and maintain its focus. Remap through source-page identity;
baseline chunk and tree-node IDs need not survive enrichment. Citations explicitly
pinned to an older snapshot continue reading that snapshot under ADR 0015.

## Consequences

The baseline must remain readable while the final snapshot is copied or a copy
is retried. Snapshot publication must avoid mixing old and new resources and
must preserve reading position and resolve existing citations. This decision
does not select snapshot addressing, artifact generation identity, notification
transport, or failure/recovery deadlines.
