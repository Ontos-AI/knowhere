# Atomically publish the retrieval-serving index

- Status: Accepted
- Context: Retrieval will use a persistent derived serving index to avoid rebuilding a large namespace on every first request. A document revision without a complete index would have unpredictable latency and could produce inconsistent scoring metadata.
- Decision: Build the serving manifest and scoring statistics in the same database transaction as the document revision. Write the completeness marker last. If serving-index construction fails, roll back the publication and retry the job; do not expose an active revision with a partial serving index.
- Consequences: Active revisions have a simple completeness invariant and predictable first-request behavior. Publication takes more work and storage, and an index failure can delay publication, but retrieval can retain a guarded legacy fallback for migrations or already-existing incomplete revisions.
