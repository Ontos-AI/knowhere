# Concurrent retrieval locking proposal

- Status: The smaller read protocol is implemented on the working branch and
  passed 65 targeted Oracle contract/native checks. Shared-row contention is
  removed from normal reads, but the 1-second warm lexical latency gate still
  fails. See the [validation report](../validation/opensearch-unlocked-reads-2026-11-04.md).
  The earlier lock-based candidate below is retained as withdrawn history.
- Date: 2026-11-04 (Pacific).
- Basis: the [ownership investigation](../validation/opensearch-pg-lock-ownership-2026-11-04.md)
  and [SQL reduction replay](../validation/opensearch-admission-round-trips-2026-11-04.md).
- Purpose: let reads in the same namespace admit concurrently while preserving
  publication eligibility, returned revision identity, exact-build collection
  and bounded recovery from native index-close races.

Execution-policy amendment: the current
[development plan](opensearch-development-plan.md#93-finite-failures-and-review-gaps)
selects one synchronous indexing execution with a configurable ten-second
limit when OpenSearch is primary, initially in the parse task and manually in
an HTTP request. PG-primary preparation/rollback uses its separately qualified
longer budget and retains the same phase/one-execution contract. Earlier
write-replay/task-scheduler recommendations below are superseded. Read-side
bounded close recovery and the reader-free coordination conclusions still
apply. Historical replay tests do not authorize automatic write retry.

## Accepted replacement behavior and proposed smaller coordination protocol

Accepted on 2026-11-04: if cleanup removes a selected old build during replacement
of the same document, return the actual hits as a normal HTTP 200 response. The
updating document may contribute fewer or no hits; the response may contain
`results: []`. Do not restart the query, switch it to the replacement to fill
missing hits, or return a special updating/conflict error for this race. This
supersedes the earlier proposal to detect publication drift and restart.

Every returned hit still binds its body, assets and citation to its selected
immutable revision/build. A later independent query selects the then-current
publication. Genuine backend failures retain their error/recovery behavior;
they are not normal no-hit responses. Independent LRU closing and reopening
remain a separate lifecycle policy. Preserving all old-build hits throughout
every in-flight request is no longer a requirement.

The implemented protocol is:

- Ordinary retrieval captures backend/epoch, namespace generation, native index
  identity and published revision/build bindings in one ordinary SQL SELECT
  without explicit business row locks, executes native search and hydrates those
  exact revisions. Do not combine old inputs with a later generation and mistake
  them for a snapshot. Cache entries retain their captured generation so an old
  request cannot populate a newer publication's cache.
- Do not add a final publication-drift check solely to reject or restart a read
  spanning replacement. An old-build filter may return ordinary empty hits.
  Keep the captured bindings for the rest of that request and return actual
  results without a replacement warning or a new public status.
- The same no-restart rule applies to lexical tool calls within an agent episode
  when replacement cleanup merely removes old hits. Do not substitute a new
  revision's body for old evidence or automatically rerun paid model work.
  Actual native failures still follow the separate backend recovery policy.
- Remove per-query revision-read/use registrations, their heartbeats, and shared
  or exclusive registry/generation row locks whose sole purpose was protecting
  the whole in-flight read. Keep actual authorization, immutable revision
  references and the normal no-hit behavior.
- Cleanup checks retirement/current publication eligibility and deletes by exact,
  non-reusable build ID. It no longer waits for reader drain. Native deletion
  can race with a reader, which returns its actual hits without version retry.
  Keep tombstones/repeated collection for late writes and failed builds.
- LRU remains independent of request transactions and may close an index that
  a caller is using. Search clients handle recognized closed/recovering errors
  through one bounded recovery. A failed write ends the indexing execution;
  only a subsequent explicit manual request may start another attempt. Access recency is a maintenance hint; it is not a per-request safety
  fence. Avoid immediate repeated eviction/reopening through LRU recency policy.
  Native access hints are coalesced in a bounded process buffer and flushed
  in one background SQL batch every 10 seconds using the API's existing async
  pool. Cache-only responses do not open or touch the native index. A query
  performs at most one closed-index recovery and replays its original build
  filters; persistent closure remains an unavailable error.
- Historical bulk replay was per-item aware and idempotent. Native IDs remain derived
  from `(buildId, unitId)` with the `index` action, allowing the same claimed
  build's batch to be replayed without duplicate records. A bulk HTTP success
  can still contain failed items. Confirm all items and search visibility before
  publication. The current single-execution policy disables automatic failed
  write replay, including this close race; retain parsed input for manual retry.
- A closed index can be reopened; a missing/corrupt index is a recovery condition,
  not permission to create an empty substitute and return success. Do not treat
  every 400/error as recoverable, and do not turn backend failure into empty hits.
- Retain conditional/short transactional coordination for document publication,
  task/attempt ownership, cleanup eligibility, backend recovery and actual index
  state/capacity transitions. The lifecycle module still owns deduplicated opens
  and the 400-slot reservation limit. These are state-change decisions, not gates
  acquired by every normal search. Worker ownership leases are distinct from
  the request-protection leases being removed.

Lifecycle reopening/recovery belongs inside Knowhere; ordinary SDK clients do
not gain revision selectors, extra job states or LRU coordination duties. This
is separate from the accepted absence of version-replacement retries. Temporary
search unavailability does not reverse a completed job's terminal state. PG
lexical retrieval remains migration rollback only, never an automatic fallback.

Official foundations: OpenSearch documents that a
[closed index rejects reads/writes and can be reopened](https://docs.opensearch.org/latest/api-reference/index-apis/close-index/),
and that [bulk items succeed/fail independently](https://docs.opensearch.org/latest/api-reference/document-apis/bulk/).
HTTP 503 with optional Retry-After is defined by
[RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#name-503-service-unavailable).
These sources do not endorse the complete application recovery protocol. The
targeted isolated checks are recorded below; production acceptance remains open.

Replace reader-drain acceptance with checks that cleanup may produce partial or
empty successful results without a version retry, updating error or mixed body/
citation identity. Replace close-exclusion acceptance with closed-index reopen,
single-execution partial-bulk failure and explicit manual-retry tests. Verify that timeouts and shard
failures are never converted to empty success. Retain stale-writer rejection,
publication atomicity, exact-build cleanup, capacity, crash-recovery and terminal
job-state checks. These targeted checks are now implemented and passed on the
isolated Oracle stack. They do not establish production capacity or latency.

## Earlier lock-based candidate — withdrawn as the current recommendation

Normal reads share the namespace lifecycle lock and insert independent use
leases. They never update the shared registry row. Index transitions take an
exclusive registry lock and check committed uses after acquiring their locks.
Read capture/renewal, publication and cleanup retain a separate generation
protocol. Expensive preparation and native HTTP stay outside those short
transactions.

This is an application protocol to verify, not an OpenSearch requirement or
an official endorsement of this complete design. PostgreSQL documents the
[compatible shared and conflicting exclusive row locks](https://www.postgresql.org/docs/14/explicit-locking.html#LOCKING-ROWS)
and the [fresh statement snapshots in Read Committed](https://www.postgresql.org/docs/14/transaction-iso.html#XACT-READ-COMMITTED)
used by the proposal. These coordination transactions require Read Committed;
verify that pool setting at initialization rather than adding a per-request
configuration query. Existing higher-isolation application sessions must not
silently substitute a stale snapshot for these short coordination transactions.

## Locks retained, weakened or removed

| Resource / operation | Proposed treatment | Reason |
| --- | --- | --- |
| Backend-control row | Keep shared locks for ordinary coordination, exclusive locks for recovery/backend fencing and the existing capacity/lifecycle controller | Normal readers coexist; old backend/node authority cannot commit through a transition. Rare controller work may still briefly block ordinary admissions. |
| Namespace generation: read capture | Keep shared, only through capture plus revision-read lease registration | A publication cannot retire the captured build before its reader is registered. |
| Namespace generation: revision-read renewal | Keep shared for the short renewal transaction | Cleanup cannot treat an old expiry as final while a timely renewal is still uncommitted. |
| Namespace generation: activation / archival | Keep exclusive for final publication mutation only | Version pointers, generation, snapshots, terminal states and outbox commit atomically. |
| Namespace generation: cleanup | Keep exclusive during its short eligibility/claim checks and existing settlement checks | Serialize collection decisions with capture/renewal and publication. Native deletion is outside this lock. |
| Namespace generation: writer claim, input preparation, before-bulk authority checks, writer-only heartbeat | Remove the unconditional exclusive generation lock | These operations do not change the published selection. Preserve their own document/job/attempt/build/lease authority locks. |
| Registry: hot read admission, hot lifecycle observation, writer-use registration | Change exclusive to shared; remove the shared-row LRU write | Readers and writers to different documents can register independent uses concurrently. No shared-to-exclusive upgrade. |
| Registry: open/close/create reservation, reconciliation that changes state/identity, lifecycle completion | Keep exclusive | These mutate the shared lifecycle and capacity authority. |
| Request's own revision/use lease rows | Keep exclusive only while validating, renewing or settling those rows | Main work, heartbeat and cancellation serialize for the same owner, not every namespace reader. |
| Document/job/attempt/build rows | Keep exclusive during actual authority/state mutations | Prevent archival, supersession, stale attempts and duplicate publication from winning incorrectly. |

The global capacity controller remains serialized in this revision. It does
not become a per-request path. Removing that rare global gate or changing the
400-slot reservation accounting is outside this proposal.
This table is the recommended scope of the next change, not a claim that
every retained lock is an irreducible minimum. The safety requirement is the
specified exclusion between conflicting operations; retaining the rare
controller gate avoids rewriting capacity management in the same change.

## Ordinary retrieval

### Capture a published revision snapshot

In one short transaction:

1. Take backend-control shared and namespace-generation shared locks.
2. Read the generation and published document/build bindings.
3. Register the owned revision-read lease for that generation; commit.

All readers can hold these shared locks together. A publisher waits only for
this short capture/registration transaction, not the native search, body
hydration or agent reasoning. No index opening/use registration is added to
cache hits or small-corpus routes that avoid native discovery.

### Admit a native operation

Outside a SQL transaction, obtain the native lifecycle observation using the
existing identity checks. Then, in a short transaction:

1. Take backend-control shared and registry shared locks.
2. Validate backend epoch, node/index identity and `open` state.
3. Lock this request's revision-read lease. After all required locks, sample
   database time and verify that the owned lease is still live.
4. Insert this request's index-use lease, recording its admission time; commit.

There is no registry update. Different requests lock different lease rows
and share the registry lock, so one admitted reader does not logically exclude
another. SQL is still required, and pool/CPU/index/WAL contention remains
possible; this is not a promise of an unlimited throughput or latency bound.

The native calls run after commit with no SQL lock or checked-out coordination
connection held. Classic reads retain their durable use protection through
body assembly and finalization. Agent reads retain the revision-read lease
across reasoning gaps and acquire/release native use around actual tool
operations; an idle index can still close between tool calls.

Final validation and release retain the current transaction/commit ownership
rules. A publication's newer generation alone does not invalidate a still-live
old reader. Native hits, hydration, bodies and citations use the captured
revision/build bindings. A cache entry is written only for a still-current
generation. Clients receive no new revision or locking responsibilities.

## Index closing and capacity

The controller keeps the existing global capacity/backend gate. Within one
short reservation transaction:

1. Obtain controller/backend exclusive authority and the target registry's
   exclusive lock.
2. After those locks have been granted, re-read registry identity/state and
   unresolved index-use leases in a subsequent statement with a fresh snapshot.
3. Any active or uncertain/unsettled use excludes the index from closing.
   Expiry alone is not proof that a remote operation stopped.
4. Otherwise reserve `closing` with its operation identity and commit.

Do not rely only on a `NOT EXISTS` predicate evaluated before a blocking row
lock. The post-lock use check is an explicit protocol step. Preliminary LRU
selection is only a hint; identity and use eligibility are checked again.

If a reader wins first, its use lease is committed before closing can pass
the exclusive gate, and the closer skips it. If the closer wins first, the
reader sees `closing` and cannot register a use. Native close/open HTTP runs
after reservation commit. Completion checks the operation ID, UUID, backend
epoch and node identity before updating the row in a second short transaction.
Failures remain uncertain/charged under the existing rules.

Opening, closing and uncertain reservations continue counting toward the cap
of 400. No active use is evicted to meet that cap. Capacity exhaustion keeps
the existing bounded wait/retryable error behavior.

## LRU and stale-observation protection

The current `lastUsedAt` field is also used to reject lifecycle observations
older than later activity/reservations. Simply throttling that field would
weaken an existing guard. Replace its hot-path writes as follows:

- Add one immutable database-time `admittedAt` value to each index-use lease.
  It is supplied in the insert already performed by admission/worker/cleanup;
  it adds no per-request statement. Add an index supporting the newest use
  timestamp for a namespace, restricted to index-use rows.
- Keep registry `lastUsedAt` as the persisted history/lifecycle watermark.
  Lifecycle reservation/completion may still update it under exclusive locks.
- Define effective last use as the later of that watermark and the newest
  retained index-use admission time. Use this value for victim ordering and
  for the existing observation-newer-than-activity checks.
- Keep active/unsettled-use eligibility independent of recency. A long-running
  use remains protected even when its admission timestamp is old.
- Before pruning released use-history rows, a bounded maintenance transaction
  transfers their maximum admission time into the registry watermark and then
  deletes them. Its exclusive registry lock makes this transfer atomic with
  new admissions. Never prune unresolved uses or lose a newer observation fence.

This preserves the previous admission-time recency semantics without updating
the same registry row on every read. It reuses the existing lease table and
maintenance scheduling. It adds no external service or client configuration.
The slow controller path must use the derived watermark everywhere the old
timestamp participated in safety decisions, not only in the LRU sort.

## Publication, input preparation and archival

Preparation reads immutable revision inputs and builds revision-specific
manifest bytes, graph features and candidate publication data outside the
generation-exclusive transaction. The candidate filename/metadata must come
from that candidate's immutable input, not the currently published document's
mutable fields.

Namespace-dependent graph edges and namespace snapshots require a base
generation. For preparation, read the backend epoch/generation before loading
the required published snapshot/peer inputs and again afterward. Proceed only
if those monotonic identities agree and every mutable input is covered by
that generation or an explicit input version/checksum. Revision-specific
inputs remain addressed by immutable revision identity. Otherwise discard the
candidate preparation and retry outside the locks. This optimistic preparation
does not replace the locked capture/reader-registration protocol for retrieval.
At activation, take the established locks in order and verify the base generation
and snapshot identity. If stale, release the transaction and rebuild outside
the locks; do not recompute a large namespace snapshot while holding the
exclusive generation lock. Such a rebase does not itself consume a failed
index-write attempt or emit `job.failed`.

The final transaction retains:

- Backend-control shared, generation exclusive and registry shared locks.
- The required document/job/attempt/build/use-lease authority locks.
- Fresh checks of archival/supersession, accepted job, attempt/build owner,
  index identity and lease validity after the locks. Native visibility is
  confirmed outside SQL first; inside the transaction compare that observation's
  build/UUID/node/epoch bindings with the owned current authority. Never make
  a native visibility HTTP call while holding SQL locks.
- Atomic application of prepared graph/manifest/snapshot changes, document
  pointer switch, generation increment, build retirement/activation, indexing
  and job terminal states, and the subscribed outbox event.

The current published version stays selected until that commit. Outbox
delivery remains after commit. Preparation/rebase stays inside the existing
background indexing lifecycle; no new SDK status, refund or public API is
introduced. Preparation retries are bounded and yield back to the existing
task scheduler rather than holding locks in a retry loop.

Writer-only claim, heartbeat and before-bulk validation skip the generation
lock, while preserving their own authority checks and row locks. If an input
mutation actually changes published pointers or namespace serving state, it
belongs to the publication/archival path and must take the exclusive generation
gate. Call-site auditing must establish this distinction before implementation.

## Cleanup and renewal

Keep cleanup's generation-exclusive eligibility transaction short. It checks
the exact retired/failed build, ensures it is not selected for publication,
and checks protected reader generations. It also registers its native use
under registry shared protection before committing. Delete only that exact
physical build outside SQL. Retired identities remain non-reusable tombstones;
late writes/partial deletion still require the existing repeated cleanup.

Revision-read renewal keeps generation shared plus the request's own lease
locks. Acquire all required owned lease rows first, then sample database time;
an expired lease cannot be revived. Cleanup's exclusive generation gate waits
for a renewal that is still committing. This prevents the following race:
a renewal started before expiry, cleanup sees the old expiry after time has
passed, then the renewal commits after cleanup admitted deletion.

If no live protected reader remains, future captures cannot select an already
retired build. Expired owners must fail final validation rather than returning
evidence. Unknown native operations retain their unresolved use protection for
close safety until known completion or valid backend/node fencing. Cancellation
still waits for both channel tasks to settle before known use release.

## Lock order and ownership

All paths use the same order, skipping unneeded resources:

1. Backend-control authority, with the required mode chosen initially.
2. Generation rows in authorization-qualified identity order, if needed.
3. Registry rows in scope order, if needed.
4. Document, job, attempt and build rows in the established order.
5. The owned revision-read lease, then owned use leases in stable ID order.

There are no shared-to-exclusive upgrades. Scope capture, native admission and
native completion remain separate short transactions; no connection is retained
over network operations or agent reasoning. Heartbeats use separate short
sessions from the existing pool. Lock policy remains inside the existing
lexical coordination module; callers do not acquire arbitrary locks themselves.
API asyncio and worker gevent use the same decisions through their existing
adapters. Classic, agent and legacy explicit sync paths must all be updated;
leaving one old registry-update path would retain a hidden serial hotspot.

The same exclusion rules cover every caller that mutates published state,
including archival, namespace moves, metadata/snapshot rebuilds and administrative
maintenance. Audit those call sites; changing only the main indexing worker
would not establish the protocol. Changes to published inputs must advance the
generation used by preparation, while final document/job/attempt guards cover
their independent ownership changes.

## Required tests before replacing the current protocol

| Controlled interleaving | Required outcome |
| --- | --- |
| Reader A pauses before admission commit; reader B enters the same namespace | B can acquire shared gates and register its different use before A is released. This tests actual overlap, not eventual completion. |
| Both readers admitted; closer requests reservation | Close cannot pass while either use remains unresolved. |
| Close reserved before reader admission | Reader cannot register a use against `closing`; normal reopening/retry applies. |
| Reader captures old publication while a new activation waits | Old snapshot and read lease commit together; publisher then switches, old reader retains body/reference identity, cleanup waits. |
| A timely renewal is held uncommitted until the old expiry passes | Collector cannot decide using the stale expiry; expired renewals never resurrect ownership. |
| Prepared namespace payload loses its base-generation race | No partial pointer/state/outbox update; release locks and reprepare. |
| Archival, supersession or lease loss occurs during preparation | Final guard rejects stale activation; current published selection remains correct. |
| Old native inventory arrives after a newer use/reservation | Recency/operation/epoch guards reject stale reconciliation, including after lease-history compaction. |
| A channel is cancelled or native completion is unknown | No premature use release or unsafe close; expiry alone does not authorize eviction. |
| Multiple cold opens/closures approach 400 slots | Reservations and uncertainty count correctly; no cap bypass or forced active eviction. |

Then rerun the existing identity/cancellation/cleanup suite and the same-quota
50-peak hot-namespace mixed replay. Record real overlap, namespace lock waits,
pool waits, errors and read/indexing latency. Separately replay the real
production namespace distribution; it must not substitute for the hotspot
case. A compatible lock diagram is not a measured throughput result.
Replace obsolete assertions that prescribe the old exact SQL layout with
the new protocol's observable concurrency and safety requirements. Statement
counts remain diagnostic evidence, not a reason to retain unnecessary CTE
complexity or contradictory old lock tests.

## Scope of the next implementation

Implement this as one coordinated protocol update: shared admission and writer
checks, lease admission timestamps and history handling, fresh close checks,
generation-lock removal from non-publication work, and shortened publication
preparation/activation. Do not deploy readers with the new timestamp protocol
while leaving the old reconciliation/maintenance decisions authoritative.
Keep protocol changes reviewable and validate race tests before load tests.

The schema addition would use a new migration; no existing migration is edited.
Existing metadata is initialized conservatively from its persisted watermark,
and mixed-version rollout needs an explicit coordination/cutover step. The
current two-hour maintenance allowance is a ceiling, not a reason to skip
verification. No production rollout or schema change is authorized by this
proposal alone.

This turn created only the proposal. No server was started, no application code
was changed, and no paid resource/provider was used. External document capture
was skipped under the existing no-new-fees constraint.
