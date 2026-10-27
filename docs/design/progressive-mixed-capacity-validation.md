# Progressive ingestion: two-worker mixed capacity validation

Date: 2026-10-27 UTC. Status: campaign C and temporary resource cleanup completed.

This follow-up evaluates the progressive branch independently of the previously
reported production deployment failure. Production services, databases and cache
contents are untouched. The completed campaign supports the tested workload on
the configured baseline, with the latency and coverage limits described below.

## Capacity decision

**The existing two-Worker/two-API allocation can process this measured mixed
workload while serving concurrent retrieval, after the two fixes below.** Worker
memory and scratch do not require an increase for these samples. CPU is the main
Worker constraint, and the database reaches its configured 4-ACU ceiling. This is
not evidence of spare capacity for a larger burst or proportional throughput from
adding more Workers against the same database cap.

The result is suitable for a release that accepts queueing under burst load. It
does **not** establish a sub-minute first-publication guarantee under 20 concurrent
ingestions: the burst's first snapshot p95 is 134 seconds, and follow-up documents
submitted during enhancement take up to 385 seconds. Increasing task-slot
concurrency alone does not add CPU or reserve capacity for new foreground work.
No new public parameter or rollout switch is introduced; the existing
`ingestion_mode=progressive` remains the selector.

This capacity verdict is independent of the earlier production deployment fault,
as requested. It does not assert that production has been deployed, that SDK/Hipo
have been released, or that a production embedding credential has the same quota.

## Code and current main integration

- Knowhere campaign C runtime: `2e5e211c455755c42df4715a8c6a91a086294183`;
  regression assertion typing: `16c798eebc05e5771c24948279ec96212a80dae7`.
  Campaigns A/B used `24730ba3c72fd20f19603139f64bace842bb8e54`.
  Remote main was refreshed and remains `6ddea434dca323fa8d3990d49eb4c3a77cd18fbe`,
  already contained in this branch.
- Node SDK: merge commit `1984c63` incorporates main `2baa533`, including stable
  2.6.0, bounded concurrent file synchronization, batched checkpoints and the ZIP
  cache. A combined progressive/cache regression first failed with two complete
  origin downloads. The merge now verifies the cached compressed object and its
  extracted entry hashes without downloading the full origin again.
  Typecheck, build, 456 SDK tests and 18 MCP tests pass. Lint has three existing
  test return-type warnings and no errors.
- Hipo: merge commit `86230595` incorporates main `32216563`, including bounded
  Blob upload buffers/concurrency and batched checkpoints. Typecheck, 2,307 unit
  tests (six skipped), 52 selected real-PostgreSQL boundary tests and three real
  Chromium progressive reader tests pass against the locally packed merged SDK.
  Checked-in dependency and lockfile retain the stable 2.6.0 pin; a release of the
  progressive SDK remains a delivery prerequisite. The public 2.6.0 package does
  not contain these branch-only progressive snapshot changes.

## Target resources and isolation

The temporary environment uses the same account, region and VPC layout as the
observed production target, with its own security group, IAM role, private S3
bucket, runtime secret, database cluster and caches. Runtime credentials have no
production database/cache access. The benchmark role is limited to its own S3
and secret plus the production image repositories needed for the test.

| Component | Fixed benchmark allocation |
| --- | --- |
| Worker | Two Fargate tasks, each 2 vCPU / 4 GiB / 21 GiB scratch, gevent concurrency 10 |
| API | Two Fargate tasks, each 0.5 vCPU / 2 GiB / 20 GiB scratch |
| PostgreSQL | Aurora PostgreSQL 15.15, one Serverless v2 writer, 0–4 ACU, auto-pause 300 seconds |
| Application cache | Separate ElastiCache Serverless Valkey 7.2, same default usage limits as production |
| Celery/RedBeat/result backend | Second ElastiCache Serverless Valkey 7.2, same default usage limits as production |

Production API/Worker image digests provide the installed Linux dependencies;
branch source is overlaid from an immutable recorded archive at task startup.
This tests the branch on the target dependency images, rather than a separately
built final release image. Resource sampling runs in a small stdlib-only child
inside each task, so its overhead is included. No autoscaling is enabled in the
isolated test: both API and Worker replica counts stay at two.

The campaign C source archive contains commit `16c798ee`; its production runtime
is identical to `2e5e211c` because the intervening change only types regression
assertions. Its compressed SHA-256 is
`202af5d5554a69f88543c82df1deafe2cdb9e8798adaea04214b58a1496bbd30`.

All Alembic migrations through `ad1e2f3a4b5c` run against an empty isolated
database. The runtime role has `NOSUPERUSER NOBYPASSRLS`. The test uses one
synthetic user with multiple namespaces, matching Hipo's shared-key/project
isolation pattern. Commercial billing and admission limits are disabled in this
environment to stress all 20 Worker slots without a tenant-tier gate.

Real text, vision, conversion and image-embedding providers are used. Image
embeddings use Hipo's existing AI Gateway credential: observed provider quota
behavior therefore does not establish the capacity of a future production
embedding credential. The Redis VLM concurrency gate remains 16 globally across
both Workers. Production telemetry and external webhooks are disabled.

## Workload and assertions

The input manifest contains 31 actual source files, 482 total physical pages:
three two-page smoke documents, four two-page canaries, a 20-document burst and
four 15-page follow-up documents submitted while the burst is still enhancing.
The burst includes eight 15-page native PDFs, four 48-page native PDFs, four
12-page image-only scanned PDFs and four 12-slide PPTX files. Follow-up jobs run
alongside enhancements rather than waiting for the burst to finish.

Fixtures derive from two public documents: the
[Attention Is All You Need paper](https://arxiv.org/pdf/1706.03762) and the
[NIST AI Risk Management Framework](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf).
Native documents have unique per-job text markers. Scan variants are rendered
into image-only PDFs and checked to contain no extracted text. PPTX variants
contain page images and unique native text. Replicas remain derivatives of these
two documents, not 31 independent samples of production document diversity.

The load generator calls the real v2 API, uploads source bytes through the
presigned S3 URL and confirms the upload. The actual Celery tasks parse, convert,
archive, upload, generate embeddings, write vectors and finalize publication.
There is no replay of prepared chunks and no mock model/embedding provider.

Classic retrieval runs open-loop at three requests per second, alternating API
replicas and four namespaces. Queries include a changing suffix to avoid warm
result-cache-only measurements. Two thirds pin a known canary document; the
remaining queries search its namespace while other documents publish. Real
agent requests are added once per minute. Separate probes rotate through the
published documents while enhancement is running. Scheduling lag, backpressure,
HTTP conflicts, errors, empty evidence and namespace leakage are recorded.

Both baseline and final archives are downloaded as soon as observed and checked
against the descriptor SHA-256, ZIP CRCs, manifest and chunk records. A separate
retained-artifact audit checks every declared page/image asset and verifies that
`source.pdf` is identical across both snapshots of each document. Database
samples check actual vector rows, generations, scope states, connection demand,
lock waits, table/index size, temporary files and deadlocks. ECS metadata and
native CloudWatch metrics record memory, CPU, scratch, ACU, I/O and cache demand.

## Invalid preliminary campaign

`campaign-a` incorrectly combined application cache and Celery into one temporary
Redis task with 0.25 vCPU / 512 MiB. ECS explicitly recorded exit 137 with
`OutOfMemoryError: container killed due to memory usage`. The subsequent VLM gate
errors, disconnected queues, HTTP 500s and request timeouts follow that loss of
Redis. That topology does not match production's separate Serverless Valkey
caches. Its availability results are excluded from the release-capacity decision.

The journals and resource evidence are retained rather than silently discarded.
All tasks from that run were stopped. The corrected `campaign-b` used two fresh
Valkey caches and a fresh database, so failed or queued work from the first run
cannot contaminate the measured campaign. No application code was changed to
address the benchmark's undersized Redis.

## Completed campaign C results

Campaign C ran from `2026-10-27T09:38:49Z` through `09:50:59Z`, 730.09 seconds.
All 31 jobs completed both snapshots; neither Worker nor API restarted or ran out
of memory. Ingestion was distributed 15/16 across the two Workers. All 163
background scopes finished refined, with no pending or running scopes left.

| Retrieval population | Requests / outcome | p50 | p95 | Maximum |
| --- | --- | --- | --- | --- |
| Classic requests, including small-corpus optimization | 1,915 HTTP 200 | 122 ms | 965 ms | 2.40 s |
| Actual hybrid `classic_topk` subset of the preceding row | 561 HTTP 200 | 705 ms | 1.27 s | 2.34 s |
| Additional document consistency probes | 520 HTTP 200 | — | 1.12 s | — |
| Actual hybrid subset of the document probes | 380 HTTP 200 | 105 ms | 1.18 s | 2.46 s |
| Agent-selected requests | 9 HTTP 200, 1 HTTP 409 | — | — | — |
| Successful real `agent_explore` episodes | 8 HTTP 200 | 21.85 s | 54.44 s | 54.44 s |

The Agent-selected population also contains one small-corpus shortcut. Its
remaining request returned `409 ABORTED` after 43.45 seconds when the namespace
changed twice. There were seven generation-change retries across the run. This
is an explicit consistency conflict, counted separately from HTTP success; the
plain HTTP generator did not retry it. The Node SDK already retries `409` only
when its canonical code is `ABORTED`, covered by its existing retry contracts.
No new client parameter is needed.

Across 2,445 requests there were no HTTP 500s, transport timeouts, successful
empty results for the known evidence probes, or cross-namespace document leaks.
The three-per-second generator incurred no backpressure; scheduling lag p95 was
2.1 ms. The additional probes covered 27 documents in all seven namespaces,
including all four mixed namespaces. Real Agent episodes ran in `mixed-0`, so
this does not claim a high-concurrency, multi-namespace Agent benchmark.
The baseline canaries deliberately exercise the small-corpus reader too; the
941 actual hybrid requests are reported separately rather than calling every
request a full BM25/vector execution.

| Phase | Documents | First snapshot p50 / p95 / maximum | Final snapshot p50 / p95 / maximum |
| --- | --- | --- | --- |
| Smoke, 2 pages each | 3 | 21.0 / 23.0 / 23.0 s | 37.5 / 51.6 / 51.6 s |
| Canaries, 2 pages each | 4 | 19.1 / 21.3 / 21.3 s | 41.3 / 48.2 / 48.2 s |
| Simultaneous 20-document burst | 20 | 107.5 / 133.9 / 144.5 s | 315.9 / 501.1 / 514.9 s |
| New jobs while enhancement occupies the Workers | 4 | 374.3 / 385.1 / 385.1 s | 456.1 / 461.3 / 461.3 s |

The follow-up jobs waited about 330 seconds before entering `running`, then
needed another 36–55 seconds to publish their first snapshot. This is an observed
foreground queueing delay on the fixed two-Worker floor, not a fast-first
latency guarantee. Existing production autoscaling was intentionally not
exercised, so its scale-out timing and throughput are not established here.

| Resource | Measured peak / limit or status |
| --- | --- |
| Worker E memory high-water | 1,435 MiB / 4,096 MiB |
| Worker F memory high-water | 1,409 MiB / 4,096 MiB |
| API G memory high-water | 1,166 MiB / 2,048 MiB |
| API H memory high-water | 1,256 MiB / 2,048 MiB |
| Worker CPU, maximum sampled interval | Approximately 100% of each 2-vCPU quota |
| Worker CPU, mean sampled intervals | 75.4% / 80.8% of quota |
| API CPU, maximum sampled interval | 76.8% / 87.7% of each 0.5-vCPU quota |
| Worker scratch, including image/runtime footprint | 3,768 MiB maximum / 21 GiB |
| API scratch, including image/runtime footprint | 4,081 MiB maximum / 20 GiB |
| Aurora capacity / CPU | 4 ACU ceiling reached; 93.7% CPU maximum |
| Database connections | 35 maximum |
| Aurora write I/O / commit latency | 6,253 IOPS maximum; 2.83 ms maximum commit latency |
| Application Valkey storage | 2.29 GiB maximum published CloudWatch value |
| Celery Valkey storage | Approximately 138 KiB maximum published value |
| Valkey throttled commands / evictions | Zero on both caches |
| Global VLM gate | Sampled maximum 16, matching the shared cap |
| Database deadlocks / temporary spill bytes | Zero / zero |

Memory high-water comes from task container counters; CPU intervals are sampled
about every ten seconds. Aurora/cache values use one-minute CloudWatch maxima.
The 21 database snapshots are about 34 seconds apart and observed no lock waits;
that does not exclude short lock contention between samples. Aurora's WAL
statistics/LSN functions are unavailable in this configuration, so no AWS WAL-byte
claim is made. Native write-I/O metrics and table sizes are retained instead.

The application cache is a material part of this workload: result caching keeps
responses for 300 seconds, including page evidence payloads. A 512-MiB standalone
cache is therefore not an equivalent substitute for the deployed Serverless
cache. This is measured total cache demand; no matched full-mode A/B run was
performed to attribute all of that demand to progressive ingestion.

## Final publication and reader artifacts

- Every mixed namespace has seven documents and serving generation 14; each smoke
  namespace has one document and generation 2. Thus serving generation advanced
  once for initial publication and once for final publication per document,
  rather than once per background scope.
- All 62 archives pass compressed SHA-256 and ZIP CRC validation. The complete
  retained-artifact audit validates 790 declared page/image assets, all required
  reader files and an identical `source.pdf` across both snapshots of each job.
- Baseline archives total 69,662,974 bytes; final archives total 243,960,706 bytes.
  Retaining the baseline adds about 66.4 MiB in this fixture set, 28.6% of final
  archive bytes. This is archive storage arithmetic, not a compute-cost uplift.
- All four rasterized burst documents have 12 initial page vectors, plus two
  smoke vectors: 50 stored 3,072-dimensional vectors. After final publication,
  46 join serving chunks. Each of the four unbound rows is physical page 4,
  explicitly recorded as an excluded TOC page in its frozen structure manifest.
  There are no unbound non-TOC pages. The initial zero-unbound assertion was too
  strict for this existing TOC exclusion rule; retained SQL results and the four
  persisted manifests establish the exception. These four small retained vector
  rows are not scored because discovery joins to serving chunks.
- The final database occupies about 125.4 MiB, including about 110.8 MiB of corpus
  tables and indexes. This is a small, freshly populated corpus, not a clone of
  the largest production namespace.

The preceding deterministic publication race contracts remain the strongest
proof of forced race ordering. This campaign adds real source processing,
provider/vector traffic, cross-process reads and publications, archive uploads,
and observed pressure on the configured resources. It does not prove maximum
1,500-page documents, an hour-long saturation soak, Worker/AZ loss recovery, or
linear capacity from the production autoscaling maximum.

## Defects found in the matching-topology campaign

Campaign B completed all 31 enhancements and verified 62 archives, 752 declared
assets and 286,986,303 compressed archive bytes. Both Workers handled ingestion
(15 and 16 documents). The campaign exposed two reproducible defects, fixed before
campaign C:

1. Image-only PDFs could be classified as `is_scanned=false` by the VLM. Three of
   four identical 12-page rasterized documents had zero native text on every page,
   but skipped the visual M0 route and published without embeddings. Stored
   blackboards, missing vector rows and live empty scoped retrieval confirmed the
   chain. The progressive parser now also selects visual M0 when the actual text
   cache contains no non-whitespace text. Native-text PDF behavior is preserved.
   The classifier-false contract failed without `_page_embedding` before the fix
   and passes after it. This is a progressive baseline routing defect; the
   classifier's general semantic output was not changed.
2. A classic query raised `OverflowError: math range error` in the importance
   sigmoid when heavily accessed evidence ranked alongside new zero-hit chunks.
   Blame traces that expression to commit `b9513b28a1` from 2026-05-16, before
   progressive ingestion. A public ranking contract reproduced the failure.
   The equivalent sigmoid now evaluates a non-positive exponential for either
   sign, preserving finite bounded weights. Both outlier-direction contracts pass.

The 28 selected parser/output/recovery/ranking contracts pass; `make check` and
explicit typechecking of the modified/new test files pass. Production fixes are
commits `92db80d8` and `2e5e211c`, with typed regression assertions in `16c798ee`.

Campaign B's additional document probe originally assumed an arbitrary filename
query must match sparse native slide text. Those empty slide-query responses are
not treated as publication failures. Campaign C fixtures carry a known unique
first-page text anchor for native PDF/PPTX probes; scanned documents use actual
risk-management queries and require visual rows. This distinguishes missing
index coverage from a query without a positive lexical match.

Three campaign B agent requests exceeded the client's 120-second timeout. These
remain failures in that journal. Campaign C uses a direct natural-language
Transformer evidence request, retains the same timeout, and records agent/sequence
identity on transport failures so their cause and latency can be assessed.

## Evidence

Raw scripts, journals and resource samples are retained under the ignored local
`.benchmarks/progressive-mixed-20261027/` directory and in the private isolated S3
bucket recorded in its `environment.json`. This state file records owned resource
identities without credential values. Do not publish the runtime secret.

The local scripts, journals, summaries, resource samples and this report are
also retained in `evidence/local-validation/evidence.tar.gz` in that bucket,
with per-file SHA-256 values in `evidence-manifest.json`. The local upload receipt
records the compressed bundle checksum. The original source archives and parsed
artifacts remain separately available in the same private bucket.

All owned ECS tasks were stopped, temporary task definitions deregistered,
both Valkey caches and the Aurora instance/cluster deleted, and the temporary
security group and IAM role removed. Runtime secret deletion has a seven-day
recovery window. Retained S3 evidence expires after 30 days; CloudWatch log
retention is seven days. `cleanup-verification.json` records the final live
resource checks. No temporary benchmark compute, database or cache remains active.

The earlier publication-only measurements and deterministic race contracts remain
in `progressive-final-publication-validation.md`; they are distinct evidence from
this full source-to-retrieval campaign.
