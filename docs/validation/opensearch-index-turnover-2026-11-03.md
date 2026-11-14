# OpenSearch index admission and turnover verification

Validated on 2026-11-03 on the `feat/wangbinqi/opensearch-publication-slice` branch.

The single node controller reserves one of 400 business index slots before
creating or opening an index. Opening, open, closing, and unresolved
operations occupy slots. The configured index prefix also reserves space
for unmanaged business indexes. The controller counts those indexes but
does not adopt or evict them. Plugin indexes use a separate resource budget.

At capacity, a cold request waits for an ongoing close or reserves the
least recently used eligible index for closure. A native use lease must be
released before its index can be closed. Expiration alone is insufficient.
The controller persists the operation ID before sending the native request,
checks the acknowledgment and actual index state, and releases capacity
only after confirming closure. A lost response leaves the slot charged,
even when a later observation shows the index closed. The native client
uses the [Close Index API](https://docs.opensearch.org/latest/api-reference/index-apis/close-index/)
and [Open Index API](https://docs.opensearch.org/latest/api-reference/index-apis/open-index/).

The existing lexical maintenance queue runs a turnover task every 30 seconds.
By default a pass starts at 360 occupied slots and closes up to 40 eligible
indexes toward 320. Index lifecycle operations use the same reservation
logic for background and foreground requests. Capacity waiting includes
transaction-local PostgreSQL lock and statement timeouts. The allocation
budget is five seconds; the native create or reopen step has its own
bounded operation budget once the slot is reserved.

The controller checks cluster identity and node incarnation using the
[Cluster State API](https://docs.opensearch.org/latest/api-reference/cluster-api/cluster-state/).
An incarnation change triggers inventory reconciliation before reuse.
An unresolved lifecycle operation is quarantined. Its reservation remains
charged until operators establish safe recovery; this proof does not
implement automatic clearance of that uncertainty.

## Evidence

| Scenario | Result |
| --- | --- |
| Real indexes at the high watermark | 360 open before the sweep; 320 open and 40 closed afterward |
| Full admission and eviction | 400 open; admitting namespace 401 left 400 open and one closed |
| Reopen with retained data | Original index UUID and searchable prepared token survived |
| Concurrent waiters for the same cold index | One eviction and one resulting native index |
| Native use and uncertain close | Expired unconfirmed use blocked eviction; loss of a real close acknowledgment kept the slot charged and blocked reopening |
| Unregistered business index and SQL contention | Unregistered index counted without eviction; a blocked allocation or SQL gate returned a retryable error within the tested wait bound |
| Incarnation reconciliation | Simulated a previous persisted incarnation against a real native inventory; an unfinished close stayed quarantined while a healthy index served |

The full capacity test used 401 real indexes and passed in 136.87 seconds.
It ran before the SQL lock timeout refinement. The final targeted run passed
32 checks in 58.98 seconds, including the bounded lock test and existing
publication, retrieval, retry, cleanup, and native bulk regressions. These
are suite wall times. The capacity run used empty indexes; the separate
reopen test used a prepared token payload.

The run used the existing Oracle host and an owned OpenSearch 3.9.0 container
with 0.7 CPU, 768 MiB JVM heap, a 1,700 MiB memory limit, and temporary local
storage. PostgreSQL fixtures used their own test database. The runtime used
the cached image, loopback access, and prepared data. No parser, metered API,
cloud storage, or paid infrastructure was used. The comparison experiment's
node, indexes, and configuration were outside this run. The owned container
and its temporary storage were removed after verification.

To reproduce on a separate test stack, set `KNOWHERE_TEST_OPENSEARCH_URL`
to the isolated node and run
`apps/api/tests/contract/test_opensearch_turnover_contract.py`. Set
`KNOWHERE_TEST_OPENSEARCH_FULL_CAPACITY=1` only for the full native count
test. The additive migration head is `1e2f3a4b5c6d`.

This validates lifecycle correctness on the isolated host. Business ranking,
the 10× mixed workload, target heap sizing, and retained-volume or S3 recovery
still need their own acceptance runs. The default backend remains PostgreSQL.
