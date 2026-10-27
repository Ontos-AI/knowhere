# Open citations on the enhanced original page

## Status

Accepted on 2026-10-27 and implemented with local verification on the same date.
SDK publication and deployed acceptance remain pending; the overall progressive
requirement remains under discussion.
Partially supersedes [ADR 0015](0015-retain-baseline-and-final-citation-snapshots.md):
snapshot retention stays; default historical-snapshot navigation changes.

## Context

Baseline and final snapshots retain the same immutable original PDF, including
the converted PDF for PPTX. Enrichment changes chunks, summaries and chapters,
not the identity of a physical source page. Pinning ordinary citation navigation
to an earlier parsed snapshot unnecessarily hides the improved reader.

## Decision

An existing citation defaults to the committed enhanced reader at the same
original file version and physical page. Before final synchronization commits,
the currently readable baseline remains available. The stable source-page
reference does not depend on a chunk ID, chapter path or parse-snapshot ID.

Retain creation-time evidence and immutable snapshots for explicit provenance
inspection only. Normal citation opening does not require choosing "latest"
first and does not rewrite the saved citation, excerpt or provenance. A replaced
original file is a different version; never silently redirect to its same-numbered
page. Multi-page evidence retains its page range and identifies the supporting
page or pages instead of assuming the first page is always the citation target.

## Consequences

Knowhere, the SDK and Hipo must carry existing page coordinates consistently to
the saved citation and the reader. Reader snapshot selection must separate
ordinary source-page navigation from explicit historical tracing. Old chunk
boundaries and tree node IDs cannot be required to locate the enhanced page.
Existing tests of default pinned-snapshot navigation do not establish acceptance
of this new default.

The local implementation follows only the committed Hipo snapshot when its
original ingestion revision matches the cited snapshot. Unknown or replacement
source versions retain the original pin. Page ranges are stored independently
of an optional selected supporting page. Reader and preview tracing is explicit,
and locating highlights in another snapshot or page never rewrites the saved
citation. Knowhere's concurrent-publication contracts assert stable source pages
and revision identity while chunk IDs change.
