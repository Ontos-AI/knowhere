# Complete progressive enrichment automatically

## Status

Accepted during the progressive-ingestion planning discussion on 2026-10-26.

## Context

Progressive ingestion publishes an initial baseline before document enrichment.
The existing implementation schedules every scope and promotes accessed scopes,
but the public request description says hit scopes are refined on demand.
Demand-only refinement would leave unused scopes incomplete and change the
meaning of document completion.

## Decision

Progressive ingestion automatically enriches the entire document, including
scopes that are never queried. Accessed scopes receive higher priority.
Retain the existing `ingestion_mode=full|progressive` interface; no additional
public parameter is needed for this behavior. Align the public description with
the existing scheduling behavior.

## Consequences

Progressive ingestion primarily reduces the time to initial usability; it does
not promise to reduce total document-processing work. Recovery must eventually
finish unaccessed scopes, and capacity validation must include full background
enrichment. The guarantees of initial Job completion, enrichment completion,
and downloadable package completion remain separate decisions to resolve.
