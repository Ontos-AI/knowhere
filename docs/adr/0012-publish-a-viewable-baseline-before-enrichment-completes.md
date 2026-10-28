# Publish a viewable baseline before enrichment completes

## Status

Accepted during the progressive-ingestion planning discussion on 2026-10-26.
Clarified on 2026-10-27: missing pre-generated native-page assets is acceptable
when the client can display the retained source PDF page by page; extracted text
alone does not satisfy original-page viewing acceptance.
Real client acceptance remains required before reporting implementation readiness.

## Context

Progressive ingestion should let a client start viewing a document while
automatic full-document enrichment continues. Hipohipo reads a synchronized
snapshot and its assets, rather than treating Knowhere Job completion alone as
client view readiness. Requiring pre-rendered images for every native page would
add initial server work, while the retained source PDF can provide the original
page's figures, tables and layout through client rendering.

## Decision

A native PDF/PPTX baseline may omit pre-generated page images if its retained
immutable source PDF can be displayed page by page in the client, including
original figures, tables and layout, before enhancement completes. PPTX uses its
converted source PDF. Page text remains useful for retrieval and degraded reading,
but text alone is not the accepted original-page viewing experience.
A scanned PDF baseline keeps its page images before complete OCR or structure
is available.

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
introduced. The acceptance flow must prove that native PDF/PPTX source pages
are viewable without pre-generated page assets or completed enhancement,
scanned PDFs render their page assets, and an incomplete client asset transfer
does not publish a ready Source. This decision establishes the intended
contract; it does not certify current progressive integration or define missing
embedding coverage, enrichment-failure policy, or snapshot refresh cadence.
