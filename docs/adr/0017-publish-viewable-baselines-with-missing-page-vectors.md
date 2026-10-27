# Publish viewable baselines with missing page vectors

## Status

Accepted during the progressive-ingestion planning discussion on 2026-10-26.

## Context

Progressive initial completion allows reading before full document enrichment.
Scanned baselines can retain all page images even when some image embeddings
fail. Rejecting the entire baseline for missing vectors would delay the accepted
viewing experience. Current code skips failed vectors, but does not durably track
or repair those gaps.

## Decision

Allow initial publication and Job `done` when the baseline and required reading
assets are available, even if page vectors are missing. Missing vectors alone
do not make initial ingestion fail. Preserve the client synchronization commit
gate: Hipo becomes readable only after its content and required assets are ready.

Record missing pages durably and schedule background repair. Do not require the
user to reopen or reingest the document to trigger repair. This decision adds no
public ingestion parameter and does not require a new queue or Worker service.

## Consequences

Initial search coverage can be incomplete while viewing is available. Later OCR
can improve text search, but cannot replace missing visual vectors. Repair must
distinguish transient provider failures from configuration problems and avoid
unbounded retries. Exact retry bounds remain implementation work. ADR 0021
requires outstanding vector repair to finish before final completion.
The separate decision to fail a search request when its required query embedding
fails remains in effect; this decision does not authorize lexical-only fallback
for such a request.
