# Reduce SQL round trips without changing lexical locking

Date: 2026-11-04 (Pacific).

The SQL reductions and safety checks passed, but the fresh mixed replay did
not demonstrate a latency improvement. Complete-pipeline P95 was 11.21 seconds
before and 11.56 seconds after; namespace admission timeouts increased from
3/50 to 7/50 attempts in this pair. This optimization remains an unqualified
PoC change, with no production rollout.

## Change

This follows the [PG ownership diagnostic](opensearch-pg-lock-ownership-2026-11-04.md).
It implements the first optimization step: fewer read-admission round trips
and reuse of the publication transaction's already-locked generation row.
Publication precomputation is a separate follow-up; graph/manifest/snapshot
work is not moved outside its current transaction in this change.

### Read admission

The backend-control shared lock, namespace-registry exclusive lock and owned
revision-read exclusive lock keep their original order and lifetime. Epoch,
node incarnation, index UUID/state, lease owner, state and expiration checks
remain in place. Native HTTP still runs outside these SQL transactions.

After the namespace lock is acquired, two statements replace four:

1. A `MATERIALIZED` CTE obtains the revision-read row `FOR UPDATE`. The outer
   projection returns that locked row and `clock_timestamp()`. A real blocked
   lease test verifies that the time is sampled after waiting for the lock,
   so a lease that expires during the wait is rejected before native search.
2. An update CTE touches the already-locked registry's LRU timestamp and
   returns its ID. The index-use insert consumes that returned ID and returns
   its new lease identity. The explicit `RETURNING` dependency combines the
   writes in one statement and transaction. Local ownership is recorded before
   the cancellable commit, preserving cancellation cleanup.

This reduces driver round trips, not the set of safety checks or state changes.
It uses PostgreSQL's documented
[CTE materialization and data-modifying statements](https://www.postgresql.org/docs/14/queries-with.html)
and SQLAlchemy's
[ORM alias/CTE mapping](https://docs.sqlalchemy.org/en/20/orm/queryguide/select.html#selecting-entities-from-subqueries).
Independent CTE text order is not relied upon to order modifications.

### Publication

`IndexingWorker._lockClaim` returns the generation object it already locked.
Activation passes it to `update_namespace_snapshot` and reuses its incremented
generation when retiring the old build and activating the new one. The snapshot
helper verifies the object's session and publication scope; the reuse path does
not accept a namespace move. Other callers keep the original locking behavior.

Generation acquisition falls from three calls to one within activation,
eliminating two duplicate create-if-missing statements and two duplicate
locked reads. Version pointers, namespace snapshot/generation, build/attempt
states, job completion and webhook outbox remain in the same transaction.
Worker execution remains gevent-compatible; its SQL changes do not alter its
execution model, retry rules, native writes or queue.

No migration, public API/SDK, scoring/tokenization, lease timing, index cap,
closing rule, cleanup rule, backend rollback or billing change is included.

## Verification

The new regressions first reported two expected failures on the previous
implementation: four admission statements after the namespace lock rather
than two, and three generation acquisitions in activation rather than one.
The real lease-lock/expiry regression passed on the previous implementation
and continues to pass with the combined query.

The final selected suite passed **70 tests, 2 deselected, in 136.12 seconds**.
It covers async cancellation, lease expiry/renewal, unknown native outcomes,
revision coexistence, cleanup exclusion, cold opening, close admission and
publication/outbox behavior. Full 400-index and external real-Redis cases
remain excluded as in the preceding diagnostic. An ORM alias type annotation
was corrected afterward without changing runtime behavior. Final targeted
Pyright reported zero errors/warnings, and Ruff passed for all changed source
and contract files. The final source hashes include the annotation correction.

The first green attempt exposed a typo in the new notification assertion
(`event_id` instead of the ORM model's `id`); this was fixed before the full
suite. The notification is intercepted only after its outbox row is persisted,
avoiding a test callback to an external provider.

## Frozen comparison protocol

An owned `knowhere-admission-h72b` native container uses the cached OpenSearch
3.9.0 image at 0.55 CPU, 1,500 MiB RAM and a 640 MiB heap. The application,
temporary PostgreSQL and indexing harness share a separate 0.55-CPU/1,700-MiB
scope. The API pool remains 12+12; indexing remains at two executions with its
existing sync pool. No connection or CPU budget is increased.

Each variant uses the same prepared 18-document corpus, initially 1,727 units
in the queried namespace, and warms all 20 business query strings. Sync/async
native Top 20 parity checks run before measurement. Both variants then replay
the identical 75-second production-derived burst profile: 50 reads concentrated
on one namespace and 40 indexing tasks split between it and a background
namespace, including a changed-body replacement. Cache and hit-stat writes are
disabled consistently. This is a 50 rolling-second peak-arrival diagnostic,
not sustained 50 QPS or a production namespace-distribution replay.

The same corrected ownership/timing probe runs in both variants. It includes
independent arrival recording, pool/SQL/commit events and temporary PG lock
samples. Its compilation and writer-context imports were preflighted; unexpected
instrumentation exceptions fail the harness. No parsing, LLM or paid provider
is invoked. The baseline restores only the three changed production files;
an exit trap restores the optimized source. All inputs and source/probe hashes
are retained under the isolated checkout's `admission-h72b` directory.

## Results

Both variants recorded an actual rolling-second peak of 50 arrivals, completed
all 40 indexing tasks successfully and reached the 75-second end marker with
no pending work. Each recorded 20/20 matching sync/async native Top 20 warmup
comparisons. All read failures were SQLSTATE `57014` statement timeouts while
acquiring the namespace registry `FOR UPDATE`, verified from transaction/SQL
events and retained temporary PG logs.

| Measurement | Before | After |
| --- | ---: | ---: |
| SQL round trips after read namespace lock | 4 | 2 |
| SQL driver executions / successful read | 21 | 19 |
| Generation acquisitions / activation | 3 | 1 |
| SQL executions / normal activation | 38 | 34 |
| SQL executions / replacement activation | 40 | 36 |
| Successful reads / attempts | 47 / 50 | 43 / 50 |
| Successful indexing tasks | 40 / 40 | 40 / 40 |
| Complete pipeline P95, all attempts | 11.205 s | 11.556 s |
| Lexical discovery P95, all attempts | 10.836 s | 11.122 s |
| Namespace-lock SQL P95, completed statements | 4.465 s | 4.618 s |
| PG pool acquisition P95 / checkout | 2.720 s | 2.903 s |
| Successful read hold P50, including commit return | 58.0 ms | 102.5 ms |
| Successful read hold P95, including commit return | 416.1 ms | 495.4 ms |
| Queried-namespace activation hold median | 639.5 ms | 385.6 ms |
| Queried-namespace activation hold maximum | 776.9 ms | 821.1 ms |

There were 39 normal activations and one changed-body replacement in each
variant. The queried-namespace activation sample contains only eight events,
so its median reduction does not establish an improvement in tail latency.
Successful-only complete-pipeline P95 equals the all-attempt P95 in each
variant. The lock SQL percentile excludes failed statements; hold percentiles
cover only the 47/43 successful admissions. These are different completed
cohorts, not paired per-request hold measurements.

Read-admission hold decomposition did not show a halving of duration:

| Mean per successful admission | Before | After |
| --- | ---: | ---: |
| Driver/SQL wall time after namespace grant | 96.60 ms | 106.58 ms |
| Time between those SQL calls | 4.41 ms | 6.41 ms |
| Commit call through return | 18.13 ms | 42.35 ms |

Removing two round trips did not remove namespace serialization, connection
queueing or scheduling delays. The extra application time between SQL calls
is too small to explain the request-level queue on its own. The observed
commit return interval also increased. These intervals include driver/client
scheduling and cannot be attributed entirely to server execution or disk I/O.
No further implementation change was made to chase a favorable single run.

The shared application/PG scope was throttled in 434/759 measured periods
before and 439/759 after. Sampled whole-host CPU averaged approximately
36.6%/37.7%, with spikes reaching 100% in both. After the baseline, the
shared-host guard observed 49.8% CPU and deferred the after variant. Source
restoration was verified, and only the after variant was resumed when its
preflight observed 17.0%. The baseline was retained. This pause and the
shared-host variation limit causal interpretation of the small latency delta;
they do not turn the increased timeout count into a passing error gate.

Both variants balanced 720/720 checkout/checkin events across API and indexing
pools. The API pool and its four checkouts per attempt were unchanged. The warm
one-second latency gate and zero-error goal remain unmet. SQL count reduction
is verified; an end-to-end performance gain is not.

### Reproduction identities

- Arrival profile SHA-256:
  `4464e84a2ad2a509bd6f233b175c1f5a107510b97deae1bb1641ff58bc25a049`.
- Before raw result SHA-256:
  `82659181f37669b46385e15bd381fed9f38da38156be23556a8fb301c1e099c9`.
- After raw result SHA-256:
  `715a36e22256e99d6aa7850586461c9562bdd6664aec2b7b2783bf2d6701fde0`.

The final `after-hashes.json` matched local and remote source after replay.
The before snapshot, corrected workload probe/hash manifest, raw events,
contracts/type logs, lock logs and detailed summaries remain in the owned
Oracle experiment directory. The failed red tests are retained as evidence
of the original four-round-trip and three-acquisition behavior.

## Interpretation and cleanup

Lock-hold durations are observed from lock response to commit completion and
include commit return latency. Driver SQL durations include server execution,
waiting, transport and client scheduling. Stage P95 values cannot be added.
The extra diagnostic events introduce overhead in both variants. A single
pair cannot establish an exact speedup or qualify production capacity.

The owned `knowhere-admission-h72b` container was stopped and its 32785 port
released. The temporary load test was removed from normal test discovery,
retaining its experiment copy. The systemd scope ended; the other chat's
18765/19203 listeners remained present. No production migration, Git commit,
deployment, paid cloud resource or paid parsing/LLM call was made. External
document parsing/upload was skipped under the no-new-fees constraint.

The next performance step should address the remaining publication work and
the serialized admission stage. Moving immutable candidate preparation out
of publication requires revision/attempt validation before atomic activation;
changing admission lock behavior requires a separate design decision. Neither
is silently included in these SQL-count changes.
