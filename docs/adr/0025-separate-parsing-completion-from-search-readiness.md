# Separate parsing completion from search readiness

- Status: Accepted for the planned OpenSearch migration; formal integration
  and production acceptance remain open.
- Evidence/delivery distinction (2026-11-04): The
  [progress ledger](../design/opensearch-implementation-progress.md) records
  isolated candidate/component evidence and its limits, not completion of every
  requirement in this ADR. Use the
  [development plan](../design/opensearch-development-plan.md) for remaining
  service, SDK, target-environment and release gates.
- Engineering reference (2026-11-03): The selected
  [implementation design](../design/opensearch-engineering-v1.md) now specifies
  separate revision pointers, phase projections, job response examples,
  and SDK wait/load/error context. Earlier open-design statements below defer
  to that reference; application implementation and acceptance remain open.
- Context: Some clients only need to display a completed parsing result,
  while others immediately search the processed revision as the next step in
  a continuous business workflow. A single undifferentiated completion signal
  would either delay result-only clients or expose search clients to incomplete
  indexing.
- Decision: Expose parsing completion independently from search readiness for
  the same document revision. Parsing completion makes the durable parsing
  result available for display or download without waiting for indexing.
  Preserve terminal job success (`done` and `job.completed`) as a signal that
  the parsing result is available and the processed revision is ready for
  retrieval. Expose the accepted `parsing_status` and `indexing_status`
  phase fields. The parsing-completion event name is `job.parsed`,
  accepted on 2026-11-02 in ADR 0028.
- Phase-state contract (accepted 2026-11-02): `parsing_status` has values
  `pending`, `running`, `completed`, and `failed`. `indexing_status` has
  values `not_started`, `pending`, `running`, `completed`, and `failed`.
  Successful parsing stays completed through indexing failures and retries.
  Indexing completion includes successful required writes, visibility,
  revision activation, and cache coherence. Explicit indexing retry may pass
  through pending/running internally, but its synchronous HTTP response reports
  final success/error instead of returning queued work.
  Overall `done` requires both phases completed and cannot regress. These
  are outcomes for the same job revision, not a live service-health signal.
  API and webhook wire names match; SDK properties follow existing language
  conventions. See the
  [phase-state contract](../design/opensearch-publication-readiness.md#accepted-phase-state-contract).
- Client contract: Document the two milestones so result-only clients can
  proceed at parsing completion and search clients can wait for overall
  completion. Overall completion must always mean that the processed revision
  is ready for retrieval. Default retrieval responses do not need warnings or
  notices about documents that have not reached search readiness.
- Completion terminology (owner-confirmed): Keep one authoritative job/phase state
  for every caller. Parsing completion, indexing completion, overall job success,
  job termination and client wait satisfaction are distinct. Overall `done`
  means both phases succeeded; `done` and `failed` are both terminal for the
  current execution. A parsed-only caller may finish its own workflow while the
  job remains running or has failed indexing, without rewriting API status or
  overall SDK `isDone`/`isFailed`. The initial worker task contains both phases;
  its normal return is not proof of job success. Event subscriptions and SDK
  waiting targets remain independent and cannot change sweep/admission rules.
  The sweep skips overall terminal jobs and checks active jobs by phase:
  parsing/upload timeout before parsing completion, indexing's own deadline
  afterward. It must not skip indexing because a parsing event was sent or a
  client already returned. See the
  [completion definitions](../design/opensearch-development-plan.md#52-phase-states-and-historical-completion)
  and [sweep classification](../design/opensearch-development-plan.md#93-finite-failures-and-review-gaps).
- Document-read contract (accepted 2026-11-02): Default document-content reads
  select the latest successfully parsed revision, even if its indexing is
  pending or failed. Job-result reads identify the corresponding job result.
  Default retrieval continues to use published revisions, and references from
  retrieval must open the revision actually retrieved. Content, assets,
  citations, metadata, and phase states must agree on that selected revision.
  These views share immutable revision data. Do not infer the newer parsed
  revision's readiness from an older published revision.
- Client responsibility (accepted 2026-11-02): Keep ordinary use simple.
  Callers can read a document by identifier without selecting revisions.
  Retrieval returns a revision-bound reference/link; the SDK propagates it,
  and direct API users can use it without reconstructing revision parameters.
  Callers need not coordinate parsing/indexing or manage publication pointers.
  Use the revision pointers and response projections in the
  [engineering design](../design/opensearch-engineering-v1.md#persistence-and-identities).
  The [read contract](../design/opensearch-publication-readiness.md#accepted-document-reads-and-client-responsibility)
  records the product rationale; implementation/acceptance remain future work.
- SDK waiting contract (option shape accepted 2026-11-02): Support Node
  `waitFor` and Python `wait_for` on both `parse()` and `jobs.wait()`, with
  values `"search_ready"` (default) and `"parsed"`. Keep overall search-ready
  completion as the default. The explicit parsing-only option returns the
  usable, persisted parsing result as soon as parsing completes.
  Background indexing continues after this early return; it does not mark
  the overall job complete or emit `job.completed`. The waiting target and
  webhook event subscriptions are independent settings. The option applies
  to the current wait invocation rather than changing a persisted job mode.
  Clients may wait for parsing and later search readiness on the same job.
  Use the current [SDK result/exception projection](../design/opensearch-engineering-v1.md#sdk-behavior);
  KW-05 verifies language-specific attributes and released-package behavior.
  The [SDK waiting contract](../design/opensearch-publication-readiness.md#accepted-sdk-waiting-behavior)
  retains the accepted waiting semantics.
  [ADR 0029](0029-keep-parsing-charges-and-allow-free-indexing-retries.md)
  applies one billing rule to every waiting mode: after successful parsing,
  indexing failure causes no refund and indexing-only retry is free. Waiting
  targets and subscriptions do not select a different billing contract.
- SDK exception contract (accepted 2026-11-02): If indexing ultimately fails
  while default `parse()` waits for overall completion, raise a job-failure
  exception carrying the indexing failure stage, original job identifier,
  and access to the retained parsing result. That immutable result must be
  readable through the ordinary SDK result-loading path despite overall job
  failure. A parsing-only wait succeeds when parsing is completed, including
  when indexing has already failed at the client's first observation. Do not
  turn this early result into overall job success. Preserve the existing Node
  `JobFailedError.jobResult` and Python `JobFailedError.job_result` context,
  with the phase fields and load behavior in the engineering design. KW-05
  completes any added language-specific attributes; the behavior is already
  selected in the [exception contract](../design/opensearch-publication-readiness.md#accepted-sdk-exception-behavior-for-indexing-failure).
- SDK waiting timeout (accepted 2026-11-02): Preserve the configurable
  polling timeout as one budget per wait invocation, without resetting it
  at parsing completion. The currently inspected local SDK defaults are
  30 minutes. On expiry, raise a distinct waiting-timeout exception with
  the original job identifier and latest known state when available.
  Background processing continues. Callers can query or resume waiting on
  the same job without re-uploading; a client timeout does not fail the
  server job, resubmit parsing, or reset the indexing execution deadline. See the
  [waiting-timeout contract](../design/opensearch-publication-readiness.md#accepted-sdk-waiting-timeout-behavior).
- Visibility basis: Use OpenSearch's officially documented search-visibility
  acknowledgment, together with complete item success and Knowhere's revision
  publication checks. Do not infer readiness from elapsed time. The owner
  accepts this visibility contract without an official end-to-end time SLA;
  thirty seconds is a reference only, not a hard completion deadline.
- Execution: [ADR 0026](0026-publish-search-readiness-from-background-tasks.md)
  records one synchronous indexing execution in the parse task and asynchronous
  phase notifications. Manual retry executes synchronously in its HTTP request.
- Consequences: Result-only clients can proceed at parsing completion, while
  search clients can continue to wait for terminal success. Readiness must
  refer to the processed revision rather than an older revision or general
  namespace health. An indexing problem must not erase an already completed
  parsing result. ADR 0026 records indexing failure notification and
  explicit indexing-only retry after the single execution fails. A parsed-only
  wait still succeeds; a search-ready wait raises on the same recorded failure.
  Detailed exception/payload properties and implementation of the accepted
  phase-state projection remain pending.
  This defines a future contract and does not claim that existing APIs already
  expose the intermediate state.
