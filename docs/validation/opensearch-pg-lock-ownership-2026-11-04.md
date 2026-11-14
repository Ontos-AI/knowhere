# PostgreSQL pool and namespace lock ownership

Report date: 2026-11-04 (Pacific).

The queue is caused by per-read exclusive admission on the namespace registry,
with additional blocking from indexing publication. It exists without indexing:
the read-only burst recorded a 3.34-second namespace-lock SQL P95. In the mixed
burst, that P95 was 4.16 seconds and four reads timed out on the same row. No
application code or locking rule was changed.

## Scope and unchanged behavior

This investigates the pool and namespace-row waits recorded by the
[async API replay](opensearch-async-api-2026-11-03.md). It changes no application
code, lock modes, lock order, SQL statements, timeouts, connection limits,
renewal rules or publication/cleanup behavior. Source hashes are compared
with the frozen async implementation before and after the investigation.

The owned Oracle integration checkout is
`<lab-home>/knowhere-opensearch-integration-20261103-c1a7`.
The new diagnostic is isolated under `lock-f29c`, including its owned native
container, loopback port, temporary PostgreSQL, logs and timing events. Native
OpenSearch retains 0.55 CPU, 1,500 MiB RAM and a 640 MiB heap. Application,
indexing harness and PostgreSQL retain their shared 0.55-CPU/1,700-MiB scope.
The API uses its existing 12+12 async pool and indexing has two execution slots.

## Why ordinary reads compete on one row

`LexicalScope.fingerprint` hashes corpus kind, owner and namespace. One
`lexical_namespace_indexes` registry row therefore represents that scope;
documents and both BM25 channels share it. Other namespaces have separate
registry rows. This diagnostic deliberately sends the burst to one namespace.

For a classic cache-miss request that actually executes native discovery,
`AsyncLexicalCoordination.admitRead` calls the existing SQL decision
`LexicalCoordination.admitObservedRead`. Its short admission transaction is:

```text
BEGIN
  configure local SQL timeouts
  lock backend-control row FOR SHARE
  lock namespace-index registry row FOR UPDATE
  verify backend epoch, cluster/node identity, index UUID and open state
  lock this request's revision-read lease FOR UPDATE
  sample database time and validate lease ownership/expiry
  insert this request's index-use lease
  update registry.lastUsedAt for LRU
COMMIT
native path/content searches, outside the transaction
```

The exclusive namespace lock remains held while the lease is checked, the
database clock is read, the use is inserted, the LRU timestamp is updated and
the transaction commits. Multiple readers therefore serialize at admission,
even though their native searches can overlap. The measured hot path has one
successful admission per classic read, not one per BM25 channel or per hydration
SQL statement. Cache hits and
small-corpus reads that skip native discovery avoid this admission.

The registry combines lifecycle/identity state, transition reservations and
LRU metadata. Admission commits the protected native use before a closer can
accept the index as idle. Revision-read leases separately protect captured
builds from cleanup. The current exclusive mode is an implementation choice
for this protocol; PostgreSQL and BM25 do not intrinsically require every
search to update one shared row. This investigation does not alter that choice.

Indexing also touches the registry: lifecycle observation and claim take
`FOR UPDATE`; claim validation, input preparation and activation use
`FOR SHARE`. These can block read admission even when no native bulk write is
in progress. Publication additionally takes the namespace-generation row
exclusively, while revision capture takes that row in shared mode.

## Prior replay evidence

The preceding fresh async run had 50 arrivals, 49 successful reads and one
namespace admission timeout. Its successful read namespace holds, measured
from SQL lock response to commit completion, had a median of 96.2 ms, P95 of
400.3 ms and maximum of 420.4 ms. Their sum was 6.198 seconds across 49
transactions. Thus a 3.70-second lock-wait P95 does not imply that one holder
kept the row for 3.70 seconds; many shorter transactions can form that queue.

That run's `pg_blocking_pids` samples predominantly listed read actors waiting
on the namespace row. These samples alone do not identify the actual holder:
PostgreSQL includes both conflicting holders and conflicting requests ahead
in the lock queue. The additional probes below distinguish those cases.

## Diagnostic method

The new replay uses the same prepared business corpus, 20 query strings,
75-second production-derived profile and actual 50 rolling-second peak
arrivals. The first variant includes the same 40 indexing tasks; the second
removes only those measured writes, retaining the same read arrivals, seeded
corpus, warmup, pool, quota and coordination protocol. There is no parsing,
LLM call, production query or production data mutation.

Temporary test-only probes record:

- Backend PID, anonymous read/write actor, operation and transaction identity.
- SQL begin/completion, namespace lock mode/target, commit begin/completion
  and rollback. Failed statements have separate error events.
- Pool acquisition begin/completion, checked-out connections and actual
  acquisition calls waiting at the configured capacity.
- Approximately 100-ms samples of `pg_stat_activity`, server wait events,
  `pg_blocking_pids`, transaction/query age and client ownership state.
- Server lock-wait logs enabled only on the temporary PostgreSQL instance.

This follows PostgreSQL's documentation for
[blocking PID semantics](https://www.postgresql.org/docs/14/functions-info.html#FUNCTIONS-INFO-SESSION-TABLE)
and [lock observation](https://www.postgresql.org/docs/14/view-pg-locks.html).
Row locks normally reside on disk; a waiter can appear as waiting for the
holder's transaction ID. Soft queue blockers must be followed to their roots.

Holder durations are client-observed bounds, not exact server grant/release
timestamps: successful lock response to commit start is a lower bound on that
observed interval, and response to commit completion includes commit return
latency. Server acquisition can precede receipt of the lock response. Samples
are not atomic with client events, so a root is attributed as a proven holder
only when its recorded held interval covers the whole server sampling query.
Samples and causal edges are not independent request counts.

The first additional probe attempt was invalidated: a diagnostic event field
named `kind` collided with the event function parameter, causing 50 TypeErrors
before read database acquisition. The second variant was interrupted. Those
artifacts are retained separately under `invalid-probe-run-1` and excluded
from every result. After that correction, a writer-annotation import failed
before all 40 writes in the next mixed attempt; that attempt is excluded and
archived under `invalid-mixed-run-2`. Its separate read-only variant succeeded
and remains the valid read-only reference. The writer-only import was fixed,
an import preflight and fail-fast checks for instrumentation exceptions were
added, and the mixed variant was rerun successfully as a diagnostic. The
read-only reference retains its exact probe source/hashes. The import and
error-check changes do not alter its read path; these are independent ownership
observations, not a precisely controlled latency-difference estimate.

## Results

| Measurement | Read-only reference | Valid mixed diagnostic |
| --- | ---: | ---: |
| Actual rolling-second read-arrival peak | 50 | 50 |
| Successful reads / attempts | 50 / 50 | 46 / 50 |
| Successful measured indexing tasks | 0 | 40 / 40 |
| Complete pipeline P95, all attempts | 6.846 s | 11.785 s |
| Namespace lock SQL P95, completed statements | 3.339 s | 4.161 s |
| Read admission hold P50, response to commit completion | 90.0 ms | 93.7 ms |
| Read admission hold P95, response to commit completion | 269.7 ms | 333.7 ms |
| Longest successful read admission hold | 571.1 ms | 527.2 ms |
| Maximum observed API read checkouts | 24 | 24 |
| Maximum observed reads waiting at the pool cap | 26 | 26 |

All four mixed failures were SQLSTATE `57014` statement timeouts on the
namespace registry `SELECT ... FOR UPDATE`. PostgreSQL logged them at
**07:57:55.346, .472, .528 and .928 UTC on 2026-11-04**, with backend PIDs
`3059721`, `3059728`, `3059723` and `3059711`, matching the client error events.
They failed before native search. The namespace SQL percentile excludes these
failed statements because no successful cursor-completion event exists for
them. This is diagnostic reproduction, not a passing latency/error gate.

Both variants reached their 75-second end markers with no pending work and
recorded 20/20 sync/async native Top 20 parity comparisons before measurement.
Read-only checkout/checkin totals matched at 200/200; mixed totals across API
and worker pools matched at 720/720, with no checkouts remaining in the final
sample. These observations do not show a connection leak.

### Actual holders

Proven root blockers include read admission transactions and indexing claim
validation transactions. Soft queue predecessors account for many apparent
blockers in a raw PID list. The read-only replay establishes that indexing
is not required to create this contention. Mixed root matches include 257
read-holder edges and six index-holder edges over the sampled observations;
these are repeated causal-edge samples, not percentages of elapsed time or
independent blocked requests.

For writes to the queried namespace, the observed registry holds were:

| Indexing phase | Mode | Transactions | Hold P50 | Hold P95 / max |
| --- | --- | ---: | ---: | ---: |
| Hot lifecycle observation | `FOR UPDATE` | 8 | 3.6 ms | 75.6 ms |
| Execution claim | `FOR UPDATE` | 8 | 59.8 ms | 102.9 ms |
| Input preparation/persistence | `FOR SHARE` | 24 | 71.7 ms | 256.0 / 300.3 ms |
| Before-bulk authority check | `FOR SHARE` | 8 | 9.6 ms | 23.6 ms |
| Revision activation/publication | `FOR SHARE` | 8 | 609.3 ms | 698.0 ms |

`IndexingWorker._lockClaim` obtains the shared namespace lock inside several
outer operations. Those phases are identified from the subsequent SQL's
operation labels. The longest queried-namespace index hold was `index:5`,
backend `3059228`, local transaction `166`, in `_activate`: **697.95 ms**.
That transaction recorded ten claim-validation SQL executions plus 28
activation/publication executions. Other long holds (`index:10`, `:20`, `:25`
and `:30`) were also activation transactions, approximately 609–686 ms. The
activation transaction includes publication metadata, graph/snapshot work,
job completion and outbox persistence. Native bulk HTTP is outside it.

The longest mixed read hold was `read:23`, backend `3059837`, approximately
**527.24 ms** from namespace-lock response to commit completion. Its lock-held
work contained exactly four subsequent SQL executions, as did every successful
read admission in both variants:

1. Lock the owned revision-read lease.
2. Read database time.
3. Insert the index-use lease.
4. Update the namespace registry's LRU timestamp.

Across the mixed read admissions, the mean interval before commit began was
118.82 ms: **114.82 ms in these four driver/SQL round trips** and **4.00 ms
between them**. Commit return added a mean 18.33 ms. Driver wall time includes
server execution, locks, network and client scheduling; it is not a measurement
of server computation alone. Holder samples repeatedly showed PostgreSQL in
`ClientRead` / idle-in-transaction between commands. There is no native HTTP
call in this admission transaction. The evidence locates the long held work
in repeated database round trips and publication SQL rather than a single
multi-second read holder.

### How namespace waiting fills the pool

At **43.658 seconds** in the mixed replay, client and server observations
showed **24 read checkouts**, **23 reads waiting on the namespace row**, and
**22 further reads waiting outside the pool**. PostgreSQL showed 22 tuple
lock waiters and one transaction-ID waiter for the namespace query; the
remaining checked-out request was processing a lease statement. The sample
and client tracing are not atomic, so this snapshot is not used to assign
that last request an exact grant/release instant.

The read-only run gave an independent snapshot at **39.214 seconds**:
24 read checkouts, 22 namespace-lock waiters and 24 outside the pool. A read
holder covered the entire sampling query. Separately, the maximum initial
pool queue was 26, during revision capture, before the namespace admission
queue had formed. Pool pressure therefore has two sources: fifty concurrent
arrivals exceeding 24 slots at capture, followed by admitted connections
remaining occupied while their transactions queue on the registry row.

The mixed pool-wait P95 was **2.852 seconds** during initial capture and
**1.574 seconds** before namespace admission. These are per-acquisition
percentiles and cannot be added to one another or to lock-wait P95. Actual
new connection establishment was separately recorded, approximately 401-ms
P95 at capture; the measurement engine is newly created despite corpus/query
warmup.

Pool identity confirms that the API's async pool (capacity 24) was used only
by read actors. Staging/indexing used another sync pool (capacity 15, with
two index execution slots). The worker did not consume API pool connections.
There was no parsing during the replay. Indexing can still delay the API
through its PostgreSQL locks and the shared CPU budget.

### CPU and retained uncertainty

The shared application/PostgreSQL scope was CPU-throttled in 425 of 760
observed periods in the mixed run and 76 of 759 in the read-only run. This
supports treating the quota as an amplifier of round-trip and critical-section
latency. CPU quota was not changed, however, and SQL driver wall time does not
separate CPU scheduling from server and transport work. Do not assign all
remaining latency to CPU or extrapolate these times to the production resource
layout.

The important architectural result is independent of that apportionment:
ordinary native reads serialize through the same per-namespace metadata row,
and waiting transactions retain connections. Async transport removes the
executor queue but preserves this serial stage. Publication adds longer
shared registry holds which conflict with read admission's exclusive mode.
Next optimization should first examine reducing SQL round trips inside read
admission and reducing work in the publication critical section while retaining
the existing lock modes, ordering, lease checks and atomic publication contract.
Changing the LRU/admission locking protocol would require a separate design
decision; it was not done here.

### Reproduction identities

- Arrival profile SHA-256:
  `4464e84a2ad2a509bd6f233b175c1f5a107510b97deae1bb1641ff58bc25a049`.
- Valid mixed raw result SHA-256:
  `67fe7474b59b5fdd178cf6e59c5d8c9ed075ee6a3ebec25948cb90a49208c8b4`.
- Valid read-only raw result SHA-256:
  `ca3decd35155f21967311ad7debb828a0baf906c7a1369e9dbeb3baafb0d687b`.

`application-hashes.json` matches the preceding frozen async source manifest.
Workload probe hashes and the separately retained read-only probe source identify
the two valid observations. Offline analysis revisions have their own
`analysis-hashes.json`; they do not change the application or replay path.

## Limits and cleanup

The extra SQL events and lock samples introduce observer overhead. These runs
attribute ownership and wait propagation; they are not a new production
latency qualification or a direct performance comparison with the earlier
lighter probe. The 50 arrivals form a short hotspot burst, not sustained 50
QPS. CPU, connection counts and locks are not varied here, so this investigation
does not separately quantify the causal contribution of the 0.55-CPU limit.

Private actor/PID timelines, frozen profile/probe hashes, source hashes, server
logs and result summaries remain in the isolated experiment directory. No raw
business queries, document IDs or credentials belong in this report.

The owned `knowhere-lock-f29c` container was stopped; its 32783 listener was
released and the systemd scope ended. The temporary test was removed from
normal test discovery, retaining its experiment copy. The other chat's
18765/19203 listeners remained present. The application source hash check
passed after both runs. No production deployment or Git commit was made.

This used the existing Oracle host, cached native image, prepared corpus,
fake/local storage and temporary PostgreSQL. No paid cloud resources or paid
API/LLM calls were created. External document parsing/upload was skipped under
the no-new-fees constraint.
