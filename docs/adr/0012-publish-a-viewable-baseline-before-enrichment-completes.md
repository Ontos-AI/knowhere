# Publish a viewable baseline before enrichment completes

## Status

Accepted during the progressive-ingestion planning discussion on 2026-10-26.
Real client acceptance remains required before reporting implementation readiness.

## Context

Progressive ingestion should let a client start viewing a document while
automatic full-document enrichment continues. Hipohipo reads a synchronized
snapshot and its assets, rather than treating Knowhere Job completion alone as
client view readiness. Requiring full page images for text-native PDFs would
add rendering work before their already-readable text becomes available.

## Decision

A text-native PDF baseline may expose page text without rendered page images.
A scanned PDF baseline exposes page images before complete OCR or structure is
available. These are accepted initial viewing experiences.

Preserve initial Knowhere Job completion and `job.completed` after the baseline
and its required resources have been durably published. Hipohipo marks its
Source ready only after the baseline snapshot and referenced assets have been
successfully synchronized. Full-document enrichment remains a separate
completion milestone and does not delay initial viewing.

After baseline synchronization makes the Source ready, allow it to participate
in Hipohipo Ask immediately. Ask can use currently published content before full
enhancement, with limited search coverage. Citations must open the corresponding
source page in the retained readable representation. Required query-embedding
failures still fail the search request under ADR 0016.

## Consequences

No additional request parameter or universal M0 page-rendering requirement is
introduced. The acceptance flow must prove that text-native PDFs render text,
scanned PDFs render their page assets, and an incomplete client asset transfer
does not publish a ready Source. This decision establishes the intended
contract; it does not certify current progressive integration or define missing
embedding coverage, enrichment-failure policy, or snapshot refresh cadence.
