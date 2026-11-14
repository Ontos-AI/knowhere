# Keep parsing charges and allow free indexing retries

- Status: Accepted for the planned OpenSearch integration, revised on
  2026-11-02; formal integration and production acceptance remain open. This
  replaces the earlier mode-dependent indexing-failure refund decision.
- Evidence/delivery distinction (2026-11-04): The
  [progress ledger](../design/opensearch-implementation-progress.md) records
  isolated candidate/component evidence and its limits, not completion of every
  requirement in this ADR. Use the
  [development plan](../design/opensearch-development-plan.md) for remaining
  service, SDK, target-environment and release gates.
- Context: Knowhere charges for parsing work. The owner chose one simple
  indexing-failure policy for all clients, regardless of how they wait for
  parsing completion and search readiness.
- Decision: Once parsing succeeds, indexing failure does not refund the
  parsing charge. If the single synchronous indexing execution fails, preserve
  the parsing result, mark indexing failed, and notify the caller. Allow
  free indexing-only retry of the same parsed revision, without reparsing
  or another parsing charge. Apply this rule to every completion/wait mode.
- Consequences: Parsing failure retains the existing normal refund behavior.
  SDK waiting targets, phase notifications, and webhook subscriptions do
  not select different billing rules. No completion-contract billing flag,
  combined-job refund branch, or post-refund indexing-retry policy is needed.
  Initial indexing runs once in the parse task; an explicit retry runs once
  in its HTTP request. ADR 0026 selects a configurable ten-second deadline
  for OpenSearch-primary work and a separately qualified longer PG-primary
  deadline; the non-gating PG shadow copy runs in a background thread. There are no automatic indexing retries; normal
  admission and rate limits still apply. There is no separate age-based retry
  expiry while the revision remains eligible and its canonical parsing result
  is available under the existing result lifecycle. Retry eligibility follows
  [ADR 0027](0027-activate-document-revisions-only-after-search-readiness.md):
  after a later update is accepted, an older failed candidate cannot be
  retried or activated. This restriction does not change the free price of
  an eligible retry. Search readiness and overall job
  completion still require successful indexing and publication checks.

This decision concerns the indexing/search-readiness stage and introduces
no retrieval-query charge. PostgreSQL's temporary lexical rollback copy
remains excluded as an OpenSearch recovery source.
