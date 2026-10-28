# Gate final enrichment completion on all required work

## Status

Accepted during the progressive-ingestion planning discussion on 2026-10-26.

## Context

Initial completion allows a viewable baseline before full enrichment and permits
missing page vectors with background repair. A distinct final completion event
must mean that the agreed retrieval-enhancement work has actually succeeded.

## Decision

Emit `job.enrichment.completed` only after text and structural enhancement has
succeeded, all recorded missing page vectors have been repaired, and the final
result package has uploaded successfully with its identity committed. This does
not require generating page vectors for documents that never use that path.

If any required stage fails after bounded retries, keep the baseline readable,
mark retrieval enhancement failed, and allow the accepted manual retry. Do not
emit the final success event or silently abandon unfinished repair.

## Consequences

The finalization gate must inspect durable stage completion, rather than only
counting refined scopes. Upload and final-event admission must recover safely
after interruption and converge without repeating successful stages or charging
again. Knowhere's final event means the upstream result is durable; Hipo only
shows local completion after the corresponding final snapshot has synchronized
and committed. This establishes workflow completion, not perfect OCR accuracy
or guaranteed relevance of every search result.
