# Fail retrieval on required query embedding errors

## Status

Accepted during the progressive-ingestion planning discussion on 2026-10-26.

## Context

Hybrid retrieval performs lexical discovery before querying the page-vector
channel. An external query embedding failure can therefore occur after lexical
results have already been found. Returning only those results would silently
omit an intended search channel during that request. Separately, the published
progressive baseline does not yet have all the text, structure, and asset
understanding of the final enriched representation.

## Decision

When a retrieval executes the page-vector channel and its required query
embedding fails after the applicable retry policy, fail the retrieval request.
Do not return lexical-only results as a successful response or interpret the
dependency failure as no matches. This policy applies to classic and agent
retrieval; tool-level exception handling must not turn this failure into a
successful incomplete answer. No new request parameter or fallback mode is
introduced.

This is a request execution policy, not a guarantee that initial publication
already provides final enrichment or complete retrieval coverage. It does not
require rejecting an otherwise viewable baseline when initial image embedding
coverage is incomplete; ADR 0017 accepts that initial publication separately.

## Consequences

Search availability depends on the required embedding provider. Preserve useful
error classification through the existing API error contract, and verify both
retrieval routes. Ordinary tool argument errors remain separate from dependency
failures. A channel skipped because it is disabled, ineligible, or has no stored
page vectors is not itself a provider error. Existing snapshot and page-asset
reading remain available. ADR 0017 records the separate publication policy for
initial page-image embedding failures during ingestion.
