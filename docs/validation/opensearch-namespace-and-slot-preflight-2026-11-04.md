# Namespace size and final slot-protocol preflight

Date: 2026-11-04, America/Los_Angeles. Branch:
`feat/wangbinqi/opensearch-publication-slice`.

Status: both bounded preflight checks passed. They support moving to formal
API/worker integration; production acceptance remains open. No application
behavior or latency configuration was optimized.

## Scope and isolation

These are the two bounded checks selected before formal API/worker integration:
the largest available prepared namespace and the complete 400-slot boundary on
the current reader-free protocol. They do not qualify production latency,
throughput, target-node heap sizing, durability or rollout.

- Existing Oracle VM only; owned checkout:
  `<lab-home>/knowhere-readfree-r8k2`.
- Owned ARM64 OpenSearch 3.9.0 container: `knowhere-scale-s8m4`, loopback 32797,
  cached image with `--pull=never`, 0.55 CPU, 1,700 MiB memory, 768-MiB fixed heap.
  The node is separate from the other chat's 18765 application and 19203 node.
- Tests and temporary PostgreSQL share a separate 0.30-CPU / 1,700-MiB scope.
  Input reconstruction is capped at 0.20 CPU; static checks at 0.15 CPU.
  Native test phases run serially. These are functional-test resource limits,
  not a new production configuration or performance comparison.
- Contract storage and pytest temporary files stay under this experiment's
  directory instead of the shared default contract directory. Test processes
  disable telemetry export and reject remote provider HTTP through loopback
  proxy settings. No parser, LLM, AWS/S3 operation or paid resource is used.
- The owned node uses the previously established absolute free-space disk
  watermarks because the host root filesystem exceeds default percentage
  watermarks. No shared-node configuration is changed.

## Frozen full namespace

The largest available retained frozen scope, `U2`, contains **42,794 real
map units from 644 documents**. Its source is the retained isolated
`knowhere-prod-20260902.dump` restore, not a fresh production export. The
approximately 45,000-unit production figure is therefore covered by a real
near-size fixture plus a separately identified derived 45,000-record check;
this is not a claim to have re-inventoried current production.
The dump-derived fixture is used for controlled validation, not as a proposed
OpenSearch backup or recovery source.

The frozen input has 5,478,333 unit/channel/term frequency records, 329,329
distinct terms, 699,564 path-token occurrences and 10,578,462 content-token
occurrences. All four frozen file SHA-256 values, per-unit token lengths and
channel frequency totals are checked. Reconstruction preserves term frequency
for whitespace BM25; original token positions are not recovered. Phrase-query
behavior is outside this check.

Every document receives a separate deterministic build ID bound to its
original document/revision identity. Native unit, section, ordering, path and
asset metadata are retained. Fixture loading uses serial 200-record raw native
bulks followed by refresh and exact visible-count verification. It is fixture
preloading, not a measurement of the application worker or its readiness queue.
The later replacement and derived-size writes use the actual
`OpenSearchLexicalIndex.writeBuild`, including `refresh=wait_for` and count checks.

### Query and evidence alignment

Use the same 20 logged business queries and original evidence labels from the
[business replay](opensearch-business-ranking-2026-11-03.md), now over the whole
frozen namespace rather than its 18-document subset. The original query log's
missing request options remain a limitation; this run freezes the same default
unfiltered scope. The full scope includes documents omitted from the earlier
sample, including the separately existing newer guideline edition.

All 19 source document/revision bindings, including the replacement source,
match the frozen scope; all 14 canonical anchor body hashes match the prepared
source files. Evidence is mapped by original document and section path before
native ranking is evaluated. One duplicated path, D415-59, is pinned to
`sec_ef87c6d473cf`: its reconstructed body overlaps 694 of 707 original anchor
tokens, versus 25 for the other path match. This alignment is recorded before
native results and does not use rank positions to choose an evidence target.

The PG comparison invokes the existing `score_persisted_corpus_many` on the
frozen query frequencies, lengths and per-channel statistics. It is an offline
PG-scoring reference, **not a live PostgreSQL SQL/latency measurement**. Final
fused-score ties retain frozen row order. In this full scope the PG reference
finds the marked evidence for **10/20 queries**, versus 13/20 in the smaller
prior fixture. Shared misses are not automatically OpenSearch regressions.

### Candidate-depth protocol

Retrieve every matching path/content hit with `search_after`, exact total-hit
counting and the adapter's deterministic sort. No writes occur during this
baseline comparison; total counts and all returned metadata are verified.
The unique unit tie-breaker and one primary/zero replicas keep this frozen
single-copy enumeration stable. No scroll/PIT context is retained.
[Official pagination behavior](https://docs.opensearch.org/latest/search-plugins/searching-data/paginate/)

From the same ordered channel inventories, apply the actual shared RRF fuser
at 100, 1,000 and all matching candidates per channel, always returning final
Top 10. Separately call the real adapter at its current default and require
identical ordered hits/scores to the 100-candidate projection. Native IDF,
BM25 parameters, channel weights and response filtering are unchanged.

The acceptance question is whether the current candidate window preserves
the marked evidence found by the PG reference. Exact PG/OpenSearch ordering
is not required. Increasing the candidate depth in this experiment does not
change the application's default.

| Final Top-10 evidence check | PG scoring reference | Native 100 | Native 1,000 | Native all matches |
| --- | ---: | ---: | ---: | ---: |
| Queries finding every marked evidence group | 10/20 | 10/20 | 10/20 | 10/20 |
| Evidence found by PG but lost by native | — | 0/10 | 0/10 | 0/10 |

All 20 default-adapter results exactly match the 100-candidate projection's
ordered units, RRF scores and metadata. Expanding to 1,000 gives identical
Top-10 order and membership for 11/20 queries. Comparing 100 with all matches,
order is identical for 9/20 and membership for 10/20. **Candidate truncation
does affect results and ordering**, although it loses no marked PG evidence in
this sample. The other ten marked evidence misses are shared by the reference
and all native depths. Do not claim that 100 candidates reproduce exhaustive
RRF or that these four intent families establish universal business relevance.
The current 100-candidate setting is unchanged.

### Size, version and lifecycle checks

The namespace contract passed in 161.87 seconds, including loading, exhaustive
enumeration, mutations and cleanup. That is not a per-request latency result.

| Scenario | Result |
| --- | --- |
| Full real fixture loaded and refreshed | Exact 42,794 visible records and expected identity metadata |
| Changed candidate tokens written before selection | New marker is absent from old published build selection, present when explicitly selecting the candidate |
| Select replacement and delete old build | New revision remains searchable; old build is empty; unrelated records remain and total count returns to 42,794 |
| Close/open the populated index | Closed reads fail as closed; original UUID, record count and selected replacement survive reopening |
| Add 2,206 explicitly derived records | Exact 45,000 visible records; padding marker is searchable only through its separate build selection |

Replacement writes and cleanup use the real native adapter. Publication
selection in this size probe is an explicit captured build tuple; it is **not
a SQL publication-transaction or full API readiness test**. Likewise, the
evidence comparison is at the lexical unit stage, not full public result
assembly. Canonical body/citation correctness remains covered by the separate
real-PG publication regressions and must be carried through formal integration.
The size test does not relocate, synthesize or overwrite canonical application
bodies/citations.

## Final reader-free protocol at 400 slots

All **8 turnover contracts passed**, including the actual full-capacity case.
The suite finished in 220.07 seconds; that is suite wall time, not an index-open
latency or throughput result.

| Check | Result |
| --- | --- |
| Actual native high watermark | 360 open, 0 closed |
| Background turnover | 320 open, 40 closed |
| Refill | 400 open, 0 closed |
| Admit namespace 401 | 400 open, 1 closed; expected idle victim closed |
| Reopen and retained token search | Original UUID and prepared searchable data retained |
| Two callers for the same cold index | One shared eviction/open operation |
| Historical native-use lease | Does not fence idle turnover under the final protocol |
| Lost close acknowledgment / pending incarnation | Uncertain operation retains its reservation and prevents unsafe reopening |
| Unmanaged index and blocked SQL allocation gate | Capacity accounting preserved; admission failure remains bounded/retryable |

The 401-index count case uses empty indexes; data-retention correctness uses
the separate prepared-token case. It does not establish memory capacity for
400 populated production indexes, nor a 400-index mixed-load latency result.
Fault-injection ERROR logs are expected and do not represent incidents on the
other chat's node.

## Reproduction and evidence

The full slot case remains in
`apps/api/tests/contract/test_opensearch_turnover_contract.py`, enabled only by
`KNOWHERE_TEST_OPENSEARCH_FULL_CAPACITY=1` on an explicitly owned node.
The full-namespace contract is
`apps/api/tests/contract/test_opensearch_namespace_scale_contract.py`, gated by
`KNOWHERE_SCALE_FIXTURE` and a loopback native endpoint.

Private inputs, reconstruction script, scope/environment plugin, logs and
reports are retained in
`<lab-home>/knowhere-readfree-r8k2/scale-s8m4`. No frozen source input is
modified. Do not blindly reuse the generated prefix/directory against a shared
node. A small local evidence copy is retained under
`/tmp/knowhere-scale-s8m4/oracle-artifacts`; the generated compressed full input
stays only on Oracle and is identified by SHA-256. The redundant full PG-score
cache is removed; the original frozen inputs and reconstruction script remain.

## Final safety, cleanup and conclusion

The additional eight real-PG/native safety contracts pass: canonical original
body and public reference revision after replacement/cleanup; ordinary empty
results when an old lexical build is removed before search; one bounded LRU
recovery and retry exhaustion; no capture DML/coordination locks; capture and
complete query progress while shared rows are held elsewhere; and replay of
the same still-owned writer build after LRU closes a partially written index.

Total: **17 contract cases passed** (eight turnover, one namespace, eight safety).
Targeted Ruff and Python-3.12 Pyright pass. All 37 monitored business source
hashes are unchanged on Oracle and locally. This turn adds a contract test and
documentation; it does not change serving code, scoring, candidate depth,
retry limits, pool sizes or the production resource configuration.

The owned node/container and anonymous volumes are removed, port 32797 is
released, all owned scopes are inactive, and private temporary PostgreSQL and
object-storage directories are removed. The other chat retains application
PID 1670349, native container ID
`b45d654ecd836e28ded8e39f47b8cd6012049e426eebb5a9a8ebc7853c0401b0`,
and native start time `2026-11-03T07:36:19.527916941Z`. Its service, index data
and configuration were not changed. `cleanup.json`, source/test hashes and
`evidence-sha256.json` record the final verification.

Both selected component preflight gaps are closed. Proceed to formal API/worker
integration while preserving the [integration constraints](../design/opensearch-integration-constraints.md).
Full public-result/SDK/webhook/authorization behavior, the target-node ten-times
mixed workload and latency gates, actual AWS durability/recovery, and migration/
rollback rehearsal remain required before production cutover. None is waived
by this preflight, and no further latency tuning is performed here.
