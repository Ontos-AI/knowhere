# Run one indexing execution inline and publish separate phase milestones

- Status: Accepted for the planned OpenSearch migration; amended by the owner
  during the document review. Formal integration and production acceptance
  remain open. This supersedes the earlier separate indexing task, three-round
  automatic retry budget and provisional 30-minute execution deadline.
- Evidence boundary: The [progress ledger](../design/opensearch-implementation-progress.md)
  describes historical candidate behavior. The
  [development plan](../design/opensearch-development-plan.md) owns current
  work and acceptance; [engineering](../design/opensearch-engineering-v1.md)
  owns the implementation protocol. The existing filename is retained for
  stable links, not as a requirement for a separate background indexing task.

## Context

Result-only clients need the completed parse promptly; search clients need a
reliable signal that its revision is published and searchable. The owner chose
one synchronous indexing execution with a short limit instead of a separate
indexing queue and automatic retry machinery. Manual recovery is sufficient
for indexing failure. Neither the API shape nor a healthy OpenSearch service
proves every input can complete within the selected limit.

## Decision

1. The existing parse task persists parsing completion, immutable result/revision,
   latest-parsed selection, initial indexing intent and subscribed `job.parsed`
   outbox event in one PG transaction. Commit and make a bounded dispatch
   attempt before starting indexing, without waiting for webhook delivery.
   Temporary notification-submission failure leaves the durable outbox for
   existing recovery; it cannot block indexing indefinitely or fail/refund the parse.
2. That same task directly invokes the indexing service once, outside the
   parsing transaction. It prepares/reuses input, connects/opens, writes all
   batches, checks every outcome and confirms search visibility before publishing.
   No initial indexing task, automatic retry task, automatic failed-build replay
   or failure-triggered background recovery execution is admitted.
   Owner-confirmed batching amendment (2026-11-05): use one
   `refresh=wait_for` bulk when the revision fits the configured serialized-byte
   budget; split oversized revisions at record boundaries as needed. All bulks
   share one execution deadline and one whole-revision publication outcome.
   Do not reset time per bulk, publish a partial candidate or force refresh.
3. For OpenSearch-primary work, persist start/owner and a fixed, configurable
   **10-second whole-execution deadline** before connection/opening. Preparation, lifecycle admission,
   writing, visibility and publication share that budget. Heartbeat/progress
   cannot extend it; large inputs have the same limit. Count admission and
   pool/lock waits from operation entry; claiming cannot restart the budget.
   Bound actual waits and
   task/request occupancy, not only a database timestamp. This is a timeout
   policy, not evidence of ten-second success for all documents.
4. Guarded publication atomically commits revision/build selection, cache
   generation, indexing/job completion and subscribed `job.completed`. Keep
   native I/O outside PG mutation transactions. Resolve an ambiguous commit
   from committed metadata; never regress an already completed job.
5. Any unsuccessful execution ends indexing as failed, retains parsing/result/
   charge, and emits subscribed `job.failed` with `failure_stage=indexing`.
   The parse task returns normally after recording this outcome. Do not raise
   indexing errors into parser autoretry/refund hooks or sleep for a retry.
6. A search-ready SDK wait raises on this failure. A parsed-only wait succeeds,
   even if indexing failed before its first poll. Both observe the same server
   state and retain result access. Waiting target does not alter execution,
   billing, subscriptions or overall `done` semantics.
7. Explicit retry uses `POST /v1/jobs/{job_id}/indexing/retry` or its v2 equivalent.
   It executes one indexing attempt synchronously **inside the HTTP request**,
   with the same selected-backend budget and no new job/task or detached continuation.
   Reuse the original parsed revision and shared indexing service. The API must
   await nonblocking I/O rather than block its event loop with worker sync I/O.
   Owner-confirmed disconnect amendment (2026-11-05): a lost client connection
   alone does not cancel the original running handler. It continues only the
   current execution within the same deadline and records the normal outcome;
   the client can query the original job. No new task or detached continuation
   is created. If that handler is actually cancelled, the process exits or its
   deadline expires, stop forward work and use bounded settlement or existing
   recovery. Preserve committed completion and already-sent native uncertainty;
   do not continue native writes/activation in detached work after cancellation.
8. Successful/already completed retry returns the normal job response with 200.
   A genuinely active unexpired execution returns 409 / `INDEXING_ALREADY_RUNNING`.
   Resolve expired state under the publication guards first, so an explicit
   retry need not wait for the periodic sweep; preserve native uncertainty.
   An admitted failure returns the existing canonical error with original job,
   phase and retained-result context: timeout 504 / `DEADLINE_EXCEEDED`,
   unavailable backend 503 / `UNAVAILABLE`, invalid internal input/mapping
   500 / `INTERNAL_ERROR`. There is no 202 queued-retry response.
   SDK retry helpers must not automatically resend this mutation after failure
   or response loss; another execution requires another explicit invocation.
9. Admit explicit retries atomically, for free and without reparsing. Preserve
   prior failures, coalesce concurrent admission, and reject archived,
   superseded or inaccessible candidates, or a lawfully unavailable canonical
   parsing result. Do not impose a separate retry-age limit: retain assigned
   indexing input while the version remains otherwise eligible and its parse
   remains available. Reclaim it when no eligible retry, PG copy or other
   required consumer needs it. Signed-URL expiry is not result/input expiry;
   unexpected assigned-input loss is an operational error. Historical completion
   remains irreversible even after old lexical cleanup. A response lost after
   commit does not authorize duplicate publication.
10. If the worker/API disappears, or execution stops between parsed commit and
    indexing start, reuse the existing stale-job sweep task and its 30-minute
    schedule for indexing failure/timeout settlement. Ordinary errors/timeouts
    remain immediate; manual retry checks expired state without waiting for
    the sweep. Add phase-aware handling using the indexing deadline or bounded
    unstarted threshold, preserve completed parsing/result/charge and publish
    the subscribed failure outcome idempotently. Do not add a watchdog or
    derive retry expiry from the original parse's age: the initial intent uses
    its creation time, while manual admission persists this request's operation
    deadline even before the execution claim. Do not
    start indexing; remove the obsolete indexing redispatch schedule. Verify
    the unstarted threshold and actual sweep lag in KW-03. Guard against newer
    attempts and committed completion; PG shadow-copy failure is not job failure.
    Timeout does not prove remote cancellation or release unresolved native
    reservations; retain stale-writer fencing and orphan cleanup obligations.
11. Keep the previous published revision selected until successful activation;
    failed/partial candidate records are never eligible hits. Archive-first
    fails unfinished indexing and rejects retry; completion-first preserves
    historical completion. See [ADR 0027](0027-activate-document-revisions-only-after-search-readiness.md).
12. Keep one callback URL and existing event selection under
    [ADR 0028](0028-select-webhook-events-on-a-single-callback-url.md).
    Parsed event persistence/dispatch precedes indexing, but network delivery
    order is not guaranteed. Manual retry does not emit a second parsed event.
    [ADR 0029](0029-keep-parsing-charges-and-allow-free-indexing-retries.md)
    preserves parsing charges and free indexing-only manual retry in all modes.

## Consequences

The owner confirmed the duplicate-delivery contract. Task delivery is
repeatable. Preserve the existing broker visibility recovery
and parsing recovery; one indexing execution per attempt does not mean one
Celery invocation. Recheck persisted phase and processing ownership before
parsing, billing or result upload. An eligible original initial attempt that
has never started can atomically claim its first execution; a repeated message
cannot restart an already-started execution or claim a later manual attempt.
Resolve committed completion or settle expired failure, without creating an
automatic indexing attempt. Lock expiry is not proof that old work stopped:
guard effects/publication against stale ownership and retain late-write cleanup.
Use scoped namespace identity and stable build/unit IDs to avoid duplicate
indexes/records, with logical outbox uniqueness independent of delivery count.
See the [redelivery contract](../design/opensearch-development-plan.md#911-duplicate-delivery-and-processing-ownership).

The common initial path avoids a second queue handoff. Removing automatic
recovery means transient failures may require an explicit client retry.
The parse task occupies its worker slot during the bounded indexing execution;
manual retry occupies its HTTP request until success/failure. Nothing about
this decision guarantees all inputs finish within ten seconds. KW-09 must
measure representative large-document and mixed-load duration, and optimize
input/batching/visibility without weakening full-publication correctness.

Reuse the existing gevent worker deployment and API; no dedicated indexing
ECS service or additional concurrency controller is selected. Preserve the
ten-times production-shaped mixed-load gate, 50 external peak retrieval QPS
and five-minute eligible catch-up gate. Focused validation uses prepared parser
output at the actual task boundary, not a standalone indexing-queue benchmark;
it does not establish full parser throughput. Repeated ten-second failures
cannot be excluded from acceptance denominators.

Maintenance/LRU, temporary PG rollback copy and webhook delivery remain
background work with their own policies. They are not automatic indexing retry.
One shared create/update-index entry point submits the PG rollback copy to a
bounded background thread pool after durable copy intent commits; saturation
or process exit leaves recoverable intent. Copies own their sessions and must
not hold shared publication locks during long COPY. No per-copy task/job is
required, and copy failure cannot downgrade OpenSearch completion.
The selected PG backend preserves the same phase/wait and one-execution
contract during preparation or deliberate rollback. Continue accepting uploads/
updates, return parsed results first where requested, and require PG publication
before search readiness. Qualify a separate longer PG budget and compatible
HTTP/gateway/SDK timeouts in KW-10; the OpenSearch ten-second default does not
apply to PG-primary work or shadow-copy duration. PG remains excluded from
runtime lexical fallback and OpenSearch disaster recovery.

The ten-second execution budget is distinct from the SDK's existing configurable
30-minute waiting timeout. The latter covers the client's parse/search-ready
wait and neither extends nor cancels server execution.
