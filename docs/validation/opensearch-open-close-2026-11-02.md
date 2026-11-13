# OpenSearch namespace index close/open timing

Raw evidence files named in this document (JSON, CSV, SQL, logs and probe copies) are retained privately and are not published in this repository.

Date: 2026-11-02, America/Los_Angeles. Collection ran from
`2026-11-03T02:16:54.397052+00:00` to
`2026-11-03T02:17:42.967817+00:00` (UTC).

Status: completed bounded local measurement. This is not production latency,
throughput, a percentile/SLA acceptance, or acceptance of Knowhere's index
lifecycle implementation. No application code or cloud resources changed.

## Result

With 370 synthetic namespace indexes, closing an idle index, opening the
target until its primary was active, and executing the first two-channel
search took median 116.9 ms for a 32-unit target and 104.3 ms for a 45,000-unit
target. The largest observed totals were 263.2 ms and 144.7 ms respectively.
Each scenario has ten observations. The larger target being faster in this
sample does not establish that larger indexes open faster.

All values below are milliseconds. Component columns are separate medians,
so they need not sum exactly to the median total. The maximum is an observed
sample maximum, not an upper bound.

| Namespace indexes | Target units | Close idle victim, median | Open target to active primary, median | First two-channel search, median | Complete swap to first search, median | Complete swap, maximum |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 3 | 32 | 27.6 | 70.7 | 10.4 | 123.9 | 181.5 |
| 3 | 45,000 | 18.8 | 78.0 | 10.8 | 108.9 | 155.4 |
| 370 | 32 | 26.4 | 83.7 | 6.3 | 116.9 | 263.2 |
| 370 | 45,000 | 22.5 | 71.5 | 8.9 | 104.3 | 144.7 |

The 370-namespace inventory also contained
3 plugin/telemetry indexes;
actual cluster index count was 373.
These were counted separately, not mistaken for namespace indexes.

## Environment and fixture

- Official ARM64 image `opensearchproject/opensearch:3.9.0`, digest
  `sha256:3b55f6b6249cfdb92b52ee59adc8a266203ba9ef9f5d82544fad6f1af2f3609e`.
- Dedicated temporary Colima VM: 2 vCPU, 4 GiB RAM. Container: 2 CPU limit,
  2 GiB memory limit, fixed 1 GiB JVM heap. Loopback endpoint only; security
  disabled solely on this isolated synthetic test node.
- One primary shard and zero replicas per namespace. No S3 remote segments,
  remote translog, remote cluster state, or independent snapshot repository.
- Whitespace analyzer and custom BM25 `k1=1.5`, `b=0.75`, using the mappings
  and HTTP helpers from the existing
  [visibility probe](../../scripts/probes/verify-opensearch-visibility.py).
- At 370 namespaces: 369 indexes with 32 units each and one with 45,000 units,
  totaling 56,808 synthetic units. Between timed swaps one namespace index
  remained open; plugin/telemetry indexes were additional. The other namespace
  indexes were closed.
- Every unit had 12 path tokens and 220 content tokens. Reserved query tokens
  were combined with deterministic synthetic words from a 65,536-term
  vocabulary, using random seed `1019 + ordinal`. No production text or
  document identifiers were read or copied, and no jieba comparison was run.
- The small target's primary store at setup was 91,453 bytes
  (89.3 KiB). The large target's primary store was
  92,596,766 bytes (88.3 MiB),
  with 7 segments at that observation. Large-target bulk input was
  78,318,280 bytes. These are fixture measurements, not estimates
  of Knowhere's production index sizes.

## Measurement procedure

Seed data in batches of at most 1,000 units with `refresh=false`, then issue
an explicit refresh to verify setup counts. Do not force-merge. Fixture
preparation is outside the timed swap.

For each measured swap:

1. Start with the 32-unit victim open and target closed, with no active
   application work using either index.
2. Close the victim and check acknowledgment.
3. Open the target with `wait_for_active_shards=1`; check both operation and
   shard acknowledgment.
4. Execute path and content searches sequentially, with `size=20`,
   `track_total_hits=true`, `_source=false`, and `request_cache=false`.
   Check zero failed shards and the full expected hit count in each channel.
5. Record end-to-end monotonic time through completion of both first searches.
   Record a subsequent warm pair separately.
6. Reset outside the timed interval by closing the target and reopening the
   victim. Repeat ten times for each target at three and at 370 namespaces.

The timing includes local HTTP round trips and primary readiness. It excludes
waiting for a busy victim, RRF computation, PostgreSQL hydration, permission
checks, SDK processing, and Knowhere task scheduling. No lifecycle controller,
lease registry, hard-cap enforcement, or application integration was built.

## Interpretation and remaining validation

The additional time for a request needing a namespace swap is:

`wait for an idle victim + close victim + open target to primary readiness + first search`

This run measures the last three terms only when the victim is already idle.
If every open namespace is busy, waiting for an operation to finish is a
separate cost that needs its own configured budget and workload validation.

These observations show subsecond operation cost on this local fixture. They
cannot establish the corresponding distribution on the accepted 4-vCPU,
32-GiB `r7g.xlarge` with 16-GiB heap, gp3 disks, private network, and S3-backed
remote durability/state. Filesystem caches were not dropped; this is local
reopening rather than a cold-disk or S3 disaster-recovery measurement.
Neither the 15-QPS retrieval burst nor three-times production indexing burst
was replayed. Most namespaces were closed, so this does not size the maximum
open-index count or validate operation under heap pressure. Closed indexes
still appeared in cluster metadata and must be included in scale validation.

Verify timing on the accepted node/durability baseline under the agreed mixed
load before choosing an open-index cap or user-facing wait budget. Do not
infer a production 0.1-second promise or a general one-to-two-second contract.

## Official basis

The [Open Index API](https://docs.opensearch.org/latest/api-reference/index-apis/open-index/)
defines waiting for active shards and exposes 30-second default timeout
parameters. The [Close Index API](https://docs.opensearch.org/latest/api-reference/index-apis/close-index/)
explains that closing blocks searches/writes and reduces in-memory indexing
and search structures while retaining disk data. The reviewed API references
do not provide a typical operation time or a Knowhere end-to-end latency
bound. A configured timeout is not a typical latency measurement.

The older [2019 Elasticsearch operational report](https://discuss.elastic.co/t/cluster-taking-a-long-time-to-open-closed-indices/169692)
quoted in the draft describes Elasticsearch 6.4.2 clusters with 12,000 or
43,000 indexes and different hardware/replication. It is a historical
firsthand observation, not an OpenSearch timing guarantee or an estimate for
370 Knowhere namespaces.

## Evidence and cleanup

- Raw final observations (`opensearch-open-close-2026-11-02/local-timing.json`).
- Forty measured swaps (`opensearch-open-close-2026-11-02/measurements.csv`).
- Resource cleanup (`opensearch-open-close-2026-11-02/cleanup.json`).
- Earlier collection attempt (`opensearch-open-close-2026-11-02/local-timing-first-pass.json`):
  stopped on an inventory assertion that incorrectly included plugin indexes
  in the namespace count. Its tiny-text timings are excluded from final
  summaries. Its cleanup assertion had the same counting error; the remaining
  indexes were plugin-owned. The final run checks only the generated prefix.

All generated namespace indexes were deleted and zero matching indexes were
confirmed before teardown. The dedicated container and anonymous volume,
temporary VM, and VM data disk were removed. The original Docker context
remained `colima`, and all 16 pre-existing containers on the original VM
remained running. No production resources, credentials, or cloud bills were
involved.
