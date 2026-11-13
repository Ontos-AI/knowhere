# Keep the PostgreSQL lexical index for migration rollback only

- Status: Accepted for the planned OpenSearch migration; clarified on
  2026-11-02. Formal integration and production acceptance remain open.
- Evidence/delivery distinction (2026-11-04): The
  [progress ledger](../design/opensearch-implementation-progress.md) records
  isolated candidate/component evidence and its limits, not completion of every
  requirement in this ADR. Use the
  [development plan](../design/opensearch-development-plan.md) for remaining
  service, SDK, target-environment and release gates.
- Rollback-copy amendment (2026-11-08): While OpenSearch serves, PostgreSQL
  term rows are no longer written continuously. A deliberate rollback rebuilds
  them for the current published revisions from the retained indexing inputs
  and verifies coverage before PostgreSQL serves. This supersedes the
  background-thread copy described below. See the
  [plan amendments](../design/opensearch-development-plan.md#owner-amendments-after-2026-11-05).
- Context: Knowhere is evaluating OpenSearch as a replacement for its custom
  PostgreSQL lexical index to improve long-term maintainability and scaling.
  Retaining the previous index is intended to support a deliberate migration
  rollback if validation shows that OpenSearch does not meet Knowhere's needs.
- Decision: Once the OpenSearch retrieval path is activated, OpenSearch is the
  only live lexical retrieval backend. The retained PostgreSQL lexical index
  must not serve as a normal or automatic fallback for indexing delays,
  OpenSearch errors, or outages, or as a permanent disaster-recovery source.
  Retention and dual writes exist only during migration evaluation. Once
  OpenSearch is accepted as stable, stop those PostgreSQL lexical writes and
  retire the obsolete lexical data. PostgreSQL remains responsible for the
  previously agreed document data and result hydration; that does not
  authorize reconstructing OpenSearch from PostgreSQL after an outage.
  This decision applies to the planned
  replacement path; existing PostgreSQL behavior is unchanged until migration.
- Rollout and retained writes (accepted 2026-11-03): Use two releases:
  prepare/backfill/reconcile while PostgreSQL continues serving, then formally
  enable OpenSearch in a nighttime maintenance window of at most two hours
  total downtime, including cutover verification and necessary restoration.
  Reserve restoration time based on rehearsal. Once OpenSearch
  serves retrieval, preserve the existing PostgreSQL lexical writer as a
  reliable background-thread copy during observation, behind the same
  create/update-index entry point. Persist copy intent before submitting it to
  a bounded thread pool, use independent sessions, and recover pending intent
  after interruption through existing maintenance; no per-copy task/job is
  needed. Keep long COPY outside shared publication locks. Its failures
  affect rollback readiness, not an already completed OpenSearch job or its
  billing. While PostgreSQL remains primary, retain its own search-readiness
  condition for completion. Verify asynchronous-copy coverage before a
  deliberate migration rollback. This does not change the accepted later
  retirement requirement. The accepted downtime budget is recorded in the
  [rollout plan](../design/opensearch-publication-readiness.md#accepted-two-release-rollout-and-asynchronous-postgresql-lexical-copy).
- Observation period (accepted 2026-11-03): Use fourteen days as the first
  internal observation period after successful formal cutover and service
  restoration. Maintain asynchronous PostgreSQL lexical writes and observe
  copy lag/failures, real retrieval quality/latency, indexing backlog, LRU
  reopening, version/namespace correctness, and backup coverage. At the review
  point, explicitly assess stability or extend observation. Stopping legacy
  writes and deleting obsolete lexical data remain separate later decisions;
  elapsed time alone does not trigger retirement. Deliberate rollback during
  observation still requires verified current copy coverage. See the
  [observation policy](../design/opensearch-publication-readiness.md#accepted-first-observation-period).
- Rollback compatibility (accepted 2026-11-03): Deliberate rollback changes
  the global lexical backend and authoritative indexing target to PostgreSQL
  while retaining phase states, SDK waiting options, webhook subscriptions,
  revision-bound results, and free eligible indexing-only retry. Under the
  current ADR 0026 amendment, initial indexing runs inline in the parse task
  and manual retry runs synchronously in its HTTP request, without automatic
  indexing retry. `done` requires committed, search-ready PostgreSQL publication
  once PostgreSQL is primary. Continue accepting new documents and updates;
  parsed results may return first, while search-ready completion waits for PG.
  Use a separately qualified longer PG execution deadline with compatible
  synchronous HTTP timeouts, rather than OpenSearch's ten-second default.
  Completed jobs remain completed, and failed
  jobs require eligible explicit retry using retained parsing results.
  Verify current published coverage and archival consistency, settle
  in-flight publication, fence late OpenSearch activation, and establish
  coherent caches before service resumes. Retain a compatible application
  release and state metadata rather than reverting the client lifecycle.
  See the [rollback scope](../design/opensearch-publication-readiness.md#accepted-migration-rollback-scope-and-client-compatibility).
- In-flight jobs during a planned transition (owner clarification): Cutover,
  deliberate rollback, or an attempted switch later aborted may interrupt
  unfinished indexing without waiting for every accepted parsing job to end.
  Fence activation, retain parsing completion/result/charge, and settle indexing
  and overall failure with the subscribed failure event and a transition
  explanation. Completed jobs retain historical success. The publication gate
  must not prevent persistence of a usable parse or invoke parser refund/retry
  hooks. After service resumes, an eligible explicit retry creates a fresh
  attempt on the selected backend/epoch; interrupted attempts never resume
  automatically, even when the backend did not change. Parsing that first
  finishes after service resumes can start its initial attempt normally.
  Preserve late-writer fencing, coverage checks and the two-hour total budget.
  See the [transition state matrix](../design/opensearch-development-plan.md#1431-in-flight-jobs-during-a-backend-transition).
- Agent retrieval failure (owner clarification): An unrecovered OpenSearch
  service failure during an agent tool call fails the whole retrieval request
  as temporarily unavailable. Do not let the model mask that failure by
  continuing through other tools or returning previously gathered evidence as
  success. Both harnesses must retain the fatal outcome through their exception
  wrappers; failed runs do not populate the normal result cache. Ordinary
  zero-hit exploration and valid cache/native-bypass paths remain unchanged;
  no per-request health probe or PG lexical fallback is added. See the
  [agent failure contract](../design/opensearch-development-plan.md#64-agent-retrieval-and-native-failure-propagation).
- Consequences: OpenSearch unavailability must produce an explicit unavailable
  or retryable outcome rather than silently routing lexical search to
  PostgreSQL. A previous document revision means a revision already indexed in
  OpenSearch, and does not provide availability during a node outage. Default
  retrieval retains that published revision while a replacement is pending
  or failed, as accepted in ADR 0027. A
  deliberate migration rollback requires verified data coverage and a
  catch-up or rebuild plan; retaining an old table alone does not guarantee a
  current migration rollback copy. OpenSearch disaster recovery must have its
  own durable data and backups and must work after the old PostgreSQL lexical
  data has been deleted. The previously proposed PostgreSQL full-rebuild
  recovery route and PostgreSQL-based snapshot catch-up are withdrawn by the
  owner's 2026-11-02 clarification.
