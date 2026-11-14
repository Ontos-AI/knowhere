# OpenSearch handoff and indexing feasibility check

Date: 2026-11-04, America/Los_Angeles. Runs began at
2026-11-05 07:45:38 UTC and 07:51:53 UTC.

Original-run status: **7 existing-candidate probes and 2 refresh controls passed, with no
skips**. The current native writer exceeded ten seconds on the largest retained
document: **the ten-second timing requirement did not pass**. A temporary
explicit-final-refresh control materially reduced that time, but it is not
`refresh=wait_for` and does not qualify the accepted production write strategy.
Application source was not changed. These are bounded development results;
formal integration and release acceptance take place in **staging**.

Follow-up on 2026-11-05: **five single-bulk `refresh=wait_for` samples all
finished below ten seconds**, while three 200-unit-batch controls all exceeded
ten seconds. The follow-up uses the identical input and retains scheduled
refresh visibility; it does not rely on the earlier explicit-refresh control.
See [the follow-up](#single-bulk-wait-for-follow-up-2026-11-05).

## Questions resolved

The [development plan](../design/opensearch-development-plan.md) now separates:

| Item | Meaning and completion point |
| --- | --- |
| Developer handoff materials | The existing candidate branch fixed to reviewed source, the existing Oracle setup/private-fixture inventory, source-matched commands and scoped results. This does not require another environment or a ZIP. KW-01 consolidates the baseline; later tasks append their outputs. |
| Ten-second indexing limit | The selected configurable whole-operation deadline, including admission, preparation, opening, writes, visibility and publication. Implement and fault-test it in KW-02/03; qualify successful completion for representative sizes/load in staging through KW-09. It is not a promise that every document succeeds within ten seconds. |
| Duplicate protection | Reuse verified component guards, then implement the new parser-entry, single-execution, explicit-retry and deadline rules. Real broker redelivery, process death and API/worker races require integrated tests. Existing guards do not make these tasks complete. |
| Formal acceptance | Staging API/worker/SDK integration, configured durability, quality, load and recovery. Oracle supplies early evidence; production deployment and observation follow staging acceptance. |

## Isolation and source identity

- Existing Oracle ARM64 host, accessed through the authorized SSH alias
  the Oracle lab host; no new host, paid provider call or image pull.
- Retained candidate export: `<lab-home>/knowhere-readfree-r8k2`. This is a
  source export without Git metadata. The local working branch is
  `feat/wangbinqi/opensearch-publication-slice`, with local base HEAD
  `fc186b185137dcf25d9d5b073dac49f1c895cacb`; HEAD alone does not identify its
  relevant modified/untracked implementation.
- Owned experiment: `<lab-home>/knowhere-handoff-20261104-9q2m`.
- Owned native container: `knowhere-handoff-9q2m`, loopback port 32817, cached
  OpenSearch 3.9.0, 0.55 CPU, 1,700-MiB memory limit and 768-MiB heap. Security
  was disabled only for this isolated loopback node. This is not a deployment
  security/durability configuration or the intended 32-GiB staging capacity.
- Image repository digest:
  `sha256:3b55f6b6249cfdb92b52ee59adc8a266203ba9ef9f5d82544fad6f1af2f3609e`;
  local ARM64 image ID:
  `sha256:4fc0ee89d630dfe0d6d330445d651d00ccc066c604f91801cbcdc387901f22f5`.
- Python 3.12.13, temporary PostgreSQL 14.18, existing locked environment.
  Drivers ran serially in user scopes capped at 0.30 CPU/1,700 MiB, with a
  360-second command limit. Redis/object-storage adapters were fakes; PostgreSQL
  and the owned native node were real. External-provider proxy paths were
  disabled and telemetry export was disabled.
- All 37 selected lexical/runtime source hashes matched before and after.
  Their saved inventory SHA-256 is
  `d277ebfc672c2c4e16ddf1f283bb5a808ceb52ca4a2461a23328ed737d9c0990`.
  The existing publication-contract module SHA-256 is
  `a9c4bf610e4fecd22b096695e60835af5e0a9b7900eb1511a7e841ada6123284`.
  This scoped identity check does not replace KW-01's complete reviewed source
  snapshot, migration and fixture inventory.

The prepared native input is retained privately at
`<lab-home>/knowhere-readfree-r8k2/scale-s8m4/native-units.private.jsonl.gz`,
SHA-256 `30ecc544259b99220894ec47b8621cd06db95cca38c9912fabc94671c9299bd4`.
It contains 42,794 units from 644 documents; the largest document has 1,862
units, serialized as approximately 1.40 MB of JSON in this probe. These are
frozen reconstructed tokens, not newly parsed documents or a fresh production
inventory. See the [fixture provenance](opensearch-namespace-and-slot-preflight-2026-11-04.md).

Read-only clarification on 2026-11-05: a **unit** here is one retrieval record
sent as one native document, containing its IDs/metadata and the pre-tokenized
`contentTokens` and `pathTokens` fields. It is not one word, one page or one
index. Counting the actual whitespace-separated input, including repetitions,
gives:

| Quantity | Count |
| --- | ---: |
| Retrieval units / native documents | 1,862 |
| Body token occurrences | 84,141 |
| Path token occurrences | 18,884 |
| Total token occurrences across both fields | **103,025** |
| Distinct body terms / distinct path terms | 10,155 / 1,995 |

Repeated occurrences are retained to represent term frequency. These counts
are neither PostgreSQL term-row counts nor LLM token counts. The approximately
1.40 MB figure describes the prepared record JSON, excluding bulk action-line
and transport overhead; it is not the original file's size. The test used the
record with the most units in the retained fixture, not a page-count selection.
It did not identify or reproduce the owner's referenced 188-page PDF as that
specific sample. Similar total word counts do not establish identical input.
The historical 188-unit/28,917-frequency-row PG/S3 experiment and synthetic
188-record visibility checks are separate evidence, not measurements of this
sample or substitutes for that PDF.

This was an actual write to the owned OpenSearch node on Oracle. Prepared
token strings were supplied to its whitespace-analyzed fields, and OpenSearch
built the native searchable index. Local fixture loading and token preparation
were outside the large-document timer; the production/shared comparison node
was not the write target.

## Existing-candidate results

The first suite passed 7 cases in 30.04 seconds. Timings below are operation
timers inside tests, not total pytest setup/fixture duration or percentiles.

| Check | Observed result | Boundary |
| --- | --- | --- |
| Single-unit publication, two independent samples | 2.09 s and 1.21 s; both completed | Existing `IndexingWorker.run` after parsed commit, including native creation/write/visibility and PG activation; excludes parsing, HTTP/queue and real object storage. |
| Three repeated calls after each completed sample | Unchanged charge/state; one result, attempt, build and execution; two subscribed logical milestone records; one native record | Calls the existing method directly; not an end-to-end Celery redelivery test. |
| Two concurrent calls for one parsed job | First completed, second observed the active claim and returned `pending`; one native write/build/execution | Paused the first native write in memory to force overlap; this is the old candidate outcome, not the new retry API response contract. |
| Late parser failure | Completed parsing and charge were preserved | Existing failure-finalizer contract. |
| Expired writer versus successor publication | Late writer could not replace the successor's published build | Historical test deliberately executes twice on one attempt. It proves stale-writer fencing, **not** the new rule that a started attempt cannot automatically restart. |
| Native failure | Raised a native error rather than returning empty results or falling back to PG | Component search boundary; does not validate fatal propagation through both agent harnesses. |
| Largest document, existing native writer | 11.00 s total; all 1,862 units visible | Native creation plus ten 200-unit-or-smaller bulks and visibility only; no preparation or PG activation. |

The last timing is a **failed ten-second feasibility observation for the
existing writer on this constrained node**. The test passed because its
assertion was exact visible-record coverage, not a ten-second deadline. Do not
report all gates green merely because pytest passed.

## Refresh control

The second suite passed 2 cases in 17.60 seconds. Both cases used the same
prepared units, native mapping, stable record IDs, ten batches, per-item checks,
fresh empty index and exact build-count validation on the same capped node.

| Strategy | Create index | Write and visibility | Native total | Visible records |
| --- | ---: | ---: | ---: | ---: |
| Existing writer: each bulk uses `refresh=wait_for` | 0.20 s | 10.47 s | **10.67 s** | 1,862 |
| Temporary control: each bulk uses `refresh=false`, then one explicit refresh before the count check | 0.18 s | 1.78 s | **1.97 s** | 1,862 |

Only the test process intercepted request paths and inserted the final refresh;
no application file or default was edited. The refresh response had no failed
shards. The unchanged writer had also measured 11.00 seconds in the first
suite, so this is direct before/control evidence of repeated visibility-wait
overhead, not a claim that faster hardware alone solves it.

This identifies a concrete implementation optimization to evaluate in KW-02/03.
Preserve final visibility, item/shard error checks, ownership/deadline fencing
and failure settlement. It does not qualify continuous concurrent writes,
refresh cost under mixed load, input compilation/download, PG activation,
remote-store durability or worker/HTTP cancellation. The final implementation
must enforce the complete ten-second budget and be measured in staging; this
small control neither changes that budget nor proves every input will succeed.

The accepted write strategy remains `refresh=wait_for`; the explicit `_refresh`
control was diagnostic, not an accepted replacement. OpenSearch documents that
`wait_for` waits for refresh visibility, while the Refresh Index API actively
refreshes the targeted shards and returns after completion. See the official
[Bulk API](https://docs.opensearch.org/latest/api-reference/document-apis/bulk/)
and [Refresh Index API](https://docs.opensearch.org/latest/api-reference/index-apis/refresh/).
The candidate serializes ten bulks of at most 200 units and waits for visibility
after each one, with a one-second configured refresh interval. Repeated waits
are a measured optimization lead; 11 seconds is not a measurement of raw
indexing CPU or proof that 103,025 word occurrences inherently require that
time. Evaluate batch/request sizing while preserving `wait_for`, then measure
the whole publication operation. **No optimized `wait_for` result or complete
ten-second deadline enforcement was validated by the 1.97-second control.**

## Reproduction and cleanup receipt

The owned experiment retains the temporary `test_handoff_probe.py`,
`test_refresh_control.py`, `handoff_environment.py`, `run-probe.sh`,
`run-control.sh`, source inventories, numeric JSON results, pytest logs and
JUnit XML. The two test-script SHA-256 values are respectively:

- `35cf20336533049c9600fa6d64e68c8b31d2827a9c69e2bbbcb483525740bde0`
- `0967d34ce264b6b1fbc5a08aacfa0de2bd8c7dec358e5cd244f62a003b6980d7`

The runners use the retained Python environment, explicit `tests.conftest` and
owned environment plugin, the owned native endpoint, private temporary PG and
storage, and the named test files. Inspect the saved runners and fixture/source
hashes before reproduction. Recreate a separate owned node with the recorded
image/resource limits and update the endpoint if its loopback port changes;
do not point the runners at shared services or staging data. The original
node has been removed, so the saved endpoint is provenance, not a live service.

At completion, the owned native container/volume and temporary test databases/
storage were removed; port 32817 was closed and no owned PG PID file remained.
The shared native container's identity/start time was unchanged and port 19203
remained listening. The shared comparison API remained listening at its bound
address `100.77.144.81:18765` (not loopback). Private source and fixture files
were retained unchanged; no shared index/service was mutated. Exact checks and
artifact hashes are retained in `verification-receipt.json`.

No application implementation, staging deployment, production test or release
was performed. The new execution policy remains scheduled implementation work;
the measurements above reduce uncertainty about reusable components and one
specific performance optimization.

## Single-bulk wait-for follow-up (2026-11-05)

The owner requested the same prepared document in one bulk, preserving
`refresh=wait_for`, and an official basis for the choice. The test ran from
08:16:04 to 08:16:51 UTC on 2026-11-05 (00:16 America/Los_Angeles).

### Official basis and implementation conclusion

The inspected candidate hard-codes 200 in the batch loop and slice. No
measured justification for that constant was found in the reviewed source or
plan. It is not an OpenSearch requirement. The official
[Bulk API batch-size guidance](https://docs.opensearch.org/latest/api-reference/document-apis/bulk/#optimal-batch-size)
says there is no universally correct operation count: choose sizes using
document size, indexing complexity, resources and network measurements. It
suggests initial experiments at 1,000–5,000 operations and roughly 5–15 MB.
Those are tuning starting points, not minimum sizes that must be filled before
sending a document. The official
[refresh guidance](https://docs.opensearch.org/latest/api-reference/index-apis/refresh/#best-practices)
supports `refresh=wait_for` when indexing must be followed by immediate search.

For this 1,862-unit document, the measured preferred approach is **one bulk
with `refresh=wait_for`**, preserving item checks and exact build visibility.
The implementation plan should replace the fixed 200-record split with a
configurable serialized-byte budget, using one request for a revision that
fits and splitting larger revisions at record boundaries when necessary.
Count UTF-8 NDJSON action lines, source fields and delimiters in that budget;
token count or plain record JSON size is insufficient. Do not delay small
documents to fill a batch, add cross-job aggregation, force refresh or enable
automatic bulk-helper retries. Qualify the general byte limit in staging;
this experiment establishes neither a universal optimum nor an unlimited
single-request policy for arbitrarily large documents.

### Controlled method

- Same frozen input SHA-256 as the original run: 1,862 retrieval units and
  103,025 body/path token occurrences. Plain prepared JSON is 1,397,098 bytes;
  actual uncompressed bulk request bodies total **1,644,744 bytes** (1.64 MB,
  approximately 1.57 MiB), including action lines and build metadata.
- Same cached ARM64 OpenSearch 3.9.0 image and resource limits: owned native
  node 0.55 CPU/1,700 MiB, 768-MiB heap; driver 0.30 CPU/1,700 MiB.
  One primary shard, no replicas, one-second configured refresh interval.
  Native remote-store durability was not configured in this component test.
- The existing writer was copied in memory with only its two batch-size
  constants changed from 200 to 1,862. Both variants retained the same mapping,
  record-ID generation, serialization, per-item response checks, native count
  check and `refresh=wait_for`. Candidate application files were unchanged.
- Three fresh-index samples per variant, interleaved in both orders, followed
  by single-bulk rewriting of the same build on an existing index and after
  close/reopen. Fresh empty index does not mean a cold JVM or a restarted node.
  No application traffic or concurrency/load campaign was introduced.
- Timers start before native-handle construction and include create/open when
  applicable, NDJSON preparation, sending the request, waiting for visibility,
  response validation and exact build-count validation. Loading/decompressing
  the retained corpus, compilation/tokenization and PG activation are excluded.
- After each operation, an immediate search with partial results disabled
  returned all 1,862 expected stable record IDs. No retry, sleep or explicit
  `_refresh` was used to make those records visible. Search-check time is
  reported separately below. Closing before the reopen sample is setup;
  reopening itself is inside its timer.

### Results

| Case | Bulk requests | Create/open + serialize/write + wait + count | Including exact-ID search verification | Visible IDs |
| --- | ---: | ---: | ---: | ---: |
| 200 per batch, fresh index 1 | 10 | 11.379 s | 11.788 s | 1,862 |
| Single bulk, fresh index 1 | 1 | 2.876 s | 2.987 s | 1,862 |
| Single bulk, fresh index 2 | 1 | 2.884 s | 3.007 s | 1,862 |
| 200 per batch, fresh index 2 | 10 | 10.819 s | 10.899 s | 1,862 |
| 200 per batch, fresh index 3 | 10 | 10.747 s | 10.784 s | 1,862 |
| Single bulk, fresh index 3 | 1 | 1.386 s | 1.418 s | 1,862 |
| Single bulk, existing index / same build | 1 | 2.396 s | 2.488 s | 1,862 |
| Single bulk, reopen / same build | 1 | 2.890 s | 2.977 s | 1,862 |

All item/visibility/identity checks passed. **Single-bulk timing passed in all
five samples; the fixed-200 strategy failed ten seconds in all three samples.**
The fresh-index medians were 2.876 s versus 10.819 s. These are small-sample
observations, not P95/P99 estimates. Stable-ID rewrites did not add records;
this does not substitute for the full job/broker duplicate-delivery contract.

The driver peak RSS was 85,132 KiB. The final native snapshot showed 52% heap
use, zero write-pool rejections and zero trips in the reported breakers; the
container had not been OOM-killed. The heap snapshot is not a measured peak,
and these observations do not qualify sustained load or final staging sizing.

This result directly answers the requested **same-data native-write** question:
one officially supported bulk plus `wait_for` fits comfortably within ten
seconds in the observed isolated cases. The complete operation's hard deadline,
prepared-input retrieval/compilation, PG activation, target durability and
mixed-load behavior still require the planned implementation and staging tests.
The old 1.97-second explicit-refresh diagnostic is unnecessary as evidence for
this chosen approach; it remains recorded only as historical diagnosis.

### Follow-up evidence and cleanup

Private evidence directory:
`<lab-home>/knowhere-single-bulk-20261105-x7n2`. It retains `probe.py`,
`run.sh`, `baseline.json`, source inventories, the original and temporary writer
functions, `results.json`, `test.log`, final node statistics and
`verification-receipt.json`. The runner uses the existing Python 3.12 environment,
disables external-provider/telemetry paths and caps execution at 240 seconds.
The whole run completed successfully with no skipped cases. Reproduction must
create a separately owned node and update the endpoint in the saved baseline;
the original endpoint is no longer live.

- Probe SHA-256:
  `0fef6b16190172b6a053b8526ecf60285945d40298611757bdef89635fdbc57b`.
- Results SHA-256:
  `ec175be1b55718ada2704eb903ff3dcc61d9562e204748c6a3e340f49aa350aa`.
- All 37 selected candidate source files matched before/after and the previous
  experiment's source inventory. The owned `knowhere-single-bulk-x7n2` node,
  its volumes and test indexes were removed; loopback port 32782 was closed.
- Shared native container identity/start time stayed unchanged. Its port 19203
  and the comparison API at `100.77.144.81:18765` remained listening.

Only temporary probe code and documentation changed. No application batching
implementation, staging deployment or production change was made.
