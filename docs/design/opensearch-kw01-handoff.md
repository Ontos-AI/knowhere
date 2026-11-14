# KW-01 reproducible component baseline

KW-01 is complete for the Phase A scope in the
[development plan](opensearch-development-plan.md#121-foundation-and-reproducibility).
The unchanged candidate was reconstructed and exercised on an isolated Oracle
stack on 2026-11-05. Its known ranking failure and real-broker skip remain open.
This record does not qualify staging, revised indexing semantics or production.

The plan review found no blocking design issue. The implementation gaps listed
by the plan remain scheduled work; reproducing the candidate does not close them.

## Source and private package

The private package is available to the authorized SSH account at:

```text
<oracle-lab-host>:<lab-home>/knowhere-kw01-20261105-01a1ac05
```

Use `ssh -o BatchMode=yes <oracle-lab-host>`. The directory is mode 0700. Private
queries, document content, raw replay reports and sample data stay outside Git.
No new parser, LLM or cloud infrastructure was used.

The matching local package, including the complete Git-base archive, is:

```text
<private-workstation-package>/opensearch-kw01
```

| Artifact | Identity and purpose |
| --- | --- |
| `source-manifest.json` | Base `fc186b185137dcf25d9d5b073dac49f1c895cacb`, branch `feat/wangbinqi/opensearch-publication-slice`; per-file sizes/hashes for 32 modified and 99 relevant untracked files, plus five dependency files. |
| `base.tar.gz`, `working-tree.patch`, `untracked.tar.gz` | Complete local baseline reconstruction. Extract the base in an empty directory, apply the patch with `git apply`, then extract the untracked archive. Verify the manifest before using it. |
| `source-code.tar.gz` | Oracle export: 1,313 source/configuration/test files, 3,283,855 bytes; SHA-256 `cdab9e86381aabf0fae37ebd0fe4e7ca2f22f6fe64f600f9603d859bb3264ba9`. Includes every candidate/dependency file. |
| `source-code-manifest.json` | Lists 1,000 omitted bundled demo binary assets. They are present in the complete local base archive and are not inputs to this component selection. |
| `source/` | Reconstructed frozen candidate. All 131 candidate and five dependency hashes matched after the campaigns. Later development uses a separate `development/` directory. |
| `oracle-fixture-inventory.private.json` | Original three fixture roots, 51 files, sizes and SHA-256 values. All original files matched after reproduction. Includes original query/evidence/chunk/replacement and historical result/log dependencies. |
| `runtime-identity.json`, `native-runtime.json` | Installed versions, PG extensions/migration head, native build/plugin inventory and observed identity. |
| `results/*-receipt.json`, `results/*.xml`, `results/*.log` | UTC times, exact commands, status, raw assertions and skips. `run-identity-map.json` resolves the early receipts to the archived first native identity; later receipts embed the native identity. |
| `handoff-index.json`, `handoff-tools-manifest.json` | Machine-readable outcome, supplement register and helper/script hashes. |

`uv.lock` SHA-256 is
`19833d58aeb7e657caf0d5439dac3a7c7fe493dc7d081db6fed6e4bb07302c30`.
The five dependency manifests and all source archives have their own hashes in
`source-manifest.json`. Preserve the original dirty worktree: these archives
include the existing candidate, rather than only HEAD or this session's edits.

## Inputs and preparation

Retained original inputs are read-only campaign dependencies:

```text
<lab-home>/knowhere-native-lexical-poc-20261028/frozen/U2
<lab-home>/knowhere-opensearch-integration-20261103-c1a7/ranking-business-c7e5
<lab-home>/knowhere-readfree-r8k2/scale-s8m4
```

The business directory contains the unchanged 20-query manifest, evidence
labels/provenance, 19 referenced original/replacement chunk files and historical
results/logs. The original manifest hash is
`445f541447028a07ac39b82b4df578384fdfdfcde48a88f5bd541b2e98f4c421`;
the historical business result hash is
`506def90edd7631f807e94470c45c0fcdfe3e0786790191e31701c2fc764271c`.
This handoff preserves that review provenance; it does not relabel evidence.

The frozen U2 inputs include units, term dictionary, postings, cases and metadata.
The scale directory includes the preparation recipe, anchor alignment, prepared
metadata and compressed native input. The latter has SHA-256
`30ecc544259b99220894ec47b8621cd06db95cca38c9912fabc94671c9299bd4`.
It contains 42,794 map units from 644 documents; the scale test also derives a
45,000-unit check. The alignment hash is
`47ddbbf64f81d8dde7d92f4bd06fd9f03f0350bf1659a8d613ead8e096693c4e`.

`prepare-oracle.py` verifies the source export and all candidate/dependency
hashes, copies business data to `fixtures/business`, and copies the four scale
inputs to `fixtures/scale`. It verifies the native-input and business-manifest
hashes against prepared metadata. Run it only in a fresh private
`<lab-home>/knowhere-kw01-*` directory containing the package inputs/helpers.
It refuses to reuse an existing `source/` directory.

`reconstruct-oracle-fixture.py` adapts the retained preparation recipe to the
owned output directory, uses original frozen U2 data and the copied business
manifest, and reuses the verified token input through a hard link to the owned
copy. It regenerates alignment/prepared metadata without parsing documents or
calling providers. `results/reconstruction.json` confirms exact equality of
every prepared metadata field, including source hashes and PG reference data;
the prepared metadata hash is
`96a03b210233e795720371275091092c88c715823544b318bc9704b7225307bd`.
Its `fixtures/reconstructed-scale` output must be absent before a fresh run.

## Runtime and isolation

Oracle is Linux 5.15.0-1081-oracle, aarch64, two CPUs and approximately 11.7 GiB
RAM. The campaign borrows the existing
`<lab-home>/knowhere-readfree-r8k2/.venv/bin/python` read-only: Python 3.12.13,
158 installed distributions, with `uv pip check` passing. `PYTHONPATH` selects
only the owned candidate source; the environment plugin asserts that import
identity. Bytecode writes and telemetry are disabled. The distribution inventory
is recorded independently of the dependency lock; this is the delivered
component runtime, not a new staging image build.

Oracle uv is 0.11.7; Node and pnpm are absent there. Local tooling was Python
3.12.13, uv 0.10.12, Node 26.7.0 and pnpm 12.4.2. SDK builds were not run on Oracle.
Use the pinned Python path for every helper, including archive extraction;
Oracle's unqualified system Python is not the delivered runtime.

PG is `/usr/lib/postgresql/14/bin/pg_ctl`, server 14.18, with `pg_trgm` 1.6,
`plpgsql` 1.0 and `uuid-ossp` 1.1. The actual isolated schema and local graph both
have the single Alembic head `1e2f3a4b5c6d`. Contract tests create their own
pytest-postgresql server on a free port in 15432–25432 and reset its test database.
They never use an existing PG service. The environment plugin also moves the
fixed contract-runtime storage roots into this task's private directory.

The native node uses cached OpenSearch 3.9.0 / Lucene 10.5.1, build
`4ee42a94e87f66fbf1e62a9871b1b87f91e02472`, image:

```text
opensearchproject/opensearch@sha256:3b55f6b6249cfdb92b52ee59adc8a266203ba9ef9f5d82544fad6f1af2f3609e
```

It is an owned single node, security disabled for loopback-only component tests,
with a random loopback port recorded in `native-identity.json`. Do not reuse a
comparison endpoint. All native phases run serially and delete only their own
indexes. Plugin versions are recorded in `runtime-identity.json`.

| Protection | Final protected campaign setting |
| --- | --- |
| Native CPU / memory / heap | 0.3 CPU / 1,700 MiB / 768 MiB; one active JVM processor |
| Native temporary data | 256 MiB data tmpfs and 512 MiB `/tmp`; counts against container memory |
| Test process group | 20% of one CPU, 1,400 MiB memory, 192 tasks |
| Temporary PG filesystem | Private systemd mount namespace, 1 GiB tmpfs; released with the test unit |
| Each native/test I/O group | Read 2 MiB/s and 50 IOPS; write 1 MiB/s and 25 IOPS, verified in its own cgroup |
| Guard | Sample every five seconds; stop this task if available memory or disk falls below 2 GiB, or the retained application fails three consecutive checks |
| PG ranking WAL | `max_wal_size=128MB`, `min_wal_size=32MB`, checkpoint timeout 30 seconds; test PG is disposable |
| Native disk watermarks | 80% low / 85% high / 90% flood; `ram-native-settings.json` records the applied settings |
| External dependencies | fakeredis and filesystem object storage; proxy denial for external provider calls, telemetry off |

The first `native` and `contracts` successes used the archived
`native-identity-before-guard.json`: 0.4 CPU, 1,700 MiB, 768 MiB heap and Docker
writable data storage, with the earlier disk-backed disposable PG layout.
The final table applies to the completed ranking/scale/slot reproduction and
KW-02 follow-up after the resource protections were tightened. Their native
identity is `native-identity.json`; `native-identity-second.json` belongs to an
intermediate stopped attempt. Do not compare timings across these layouts.
The original test memory ceiling was 1,100 MiB; the later 1,400 MiB adjustment
is recorded separately. `run-identity-map.json` keeps the early receipt mapping
explicit without rewriting the original receipts.

These RAM filesystems, disposable PG settings and small resource ceilings are
functional isolation measures. They provide no native durability, production
throughput or ten-second whole-execution qualification.

Early attempts triggered two guard stops: insufficient free disk during
unbounded disposable-PG WAL growth, then three health-check timeouts from the
old native comparison endpoint. Receipts are retained. PG was moved to the
private RAM filesystem with bounded WAL and I/O; the owner then authorized
retirement of the old validation node. An initial absolute disk-watermark setting
was also unsuitable for a 256 MiB tmpfs; applying the recorded relative values
restored writable admission. Setup failures remain archived separately from the
completed baseline. The final helpers contain these corrections.

## Reproduction commands and expected outcomes

The package is already prepared at the private path. For a fresh reproduction,
copy its source export, manifests, fixture inventory and Python helpers to a new
private task root, then run `prepare-oracle.py` using the pinned interpreter.
Only one campaign may own the fixed task unit/container names at a time. After
the previous campaign is cleaned up, start the guard before the native node:

```bash
ROOT=<lab-home>/knowhere-kw01-20261105-01a1ac05
PYTHON=<lab-home>/knowhere-readfree-r8k2/.venv/bin/python
sudo -n systemd-run --unit=knowhere-kw01-guard --uid=<lab-user> \
  --property=CPUQuota=5% --property=MemoryMax=64M --property=TasksMax=32 \
  --collect "$PYTHON" "$ROOT/guard-oracle.py"
"$PYTHON" "$ROOT/start-oracle-node.py"
"$PYTHON" "$ROOT/await-oracle-node.py"
"$PYTHON" "$ROOT/launch-oracle-tests.py" native contracts ranking scale slots
```

Use the new root in `ROOT` for a fresh reproduction. A retained `guard-stop`
sentinel is a cleanup result, so a fresh campaign must use a fresh task root.
The native port is discovered by the helpers, not hard-coded. Readiness may take
several minutes under the I/O cap; inspect only the owned container and the
recorded guard outcome if startup does not finish. Never bypass a guard trip.

The launcher establishes systemd bounds and actual scoped cgroup I/O limits
before admitting tests with a matching token. Read results after the transient
unit exits. The runner uses a 1,200-second outer bound for each phase, separate
from application deadlines. It continues past only the explicitly known ranking
assertion and ultimately retains a nonzero campaign outcome for that failure.
The exact pytest selectors/variables are in `run-oracle-baseline.py` and receipts.

| Phase | Reproduced outcome |
| --- | --- |
| `native` | 10 passed: three native protocol/visibility checks plus seven webhook subscription checks. |
| `contracts` | 66 passed, two skipped: full-slot opt-in and the separate real Redis/Celery/gevent worker case. |
| `ranking` | One known failure, no setup errors/skips. All 12 stages completed: 240 query/stage comparisons over 20 unique queries; zero losses of PG-found evidence groups, 84 shared missing groups. The final absolute-evidence assertion fails as declared in the plan. |
| `scale` | One passed: 42,794-unit real namespace, candidate-depth/revision checks and derived 45,000-unit check. |
| `slots` | Eight passed with full-capacity opt-in, resolving the earlier slot skip; seven cases overlap the `contracts` selection. |
| `prepare` | Exact prepared-input reconstruction passed; run separately with the launcher after ensuring its output directory is absent. |
| `verify` | All 131 source files, five dependency files and 51 original private fixture files matched; all 13 unrelated containers kept their original IDs/start times, and the retained application returned 200. |

Do not add table counts as unique coverage: the selections overlap. The real
broker test remains unexecuted for the revised workflow and belongs to KW-02.
The ranking evaluator is preserved unchanged; its correction and separate
relative-quality qualification belong to KW-09. Local `make check` passed
Ruff and Pyright with zero errors/warnings.

## Staging, SDK and compatibility inventory

Read-only ECS inventory found `knowhere-fargate` in `us-east-1` with both staging
services at desired/running/pending zero. No service was started:

| Service | Task definition | CPU / memory | Image digest |
| --- | --- | --- | --- |
| API | `knowhere-api-staging:52` | 256 / 1,024 MiB, X86_64 | `sha256:a998bb6338862ac2c6b4af32f7f278aa1d9bd0064cef868b806089d561653810` |
| Worker | `knowhere-worker-staging:55` | 2,048 / 4,096 MiB, X86_64; declared concurrency 10 | `sha256:bf664fc9821a616288c87881c2681943a6211b16d22bbeb5ca04c4893b55c579` |

Stopped task definitions do not prove effective live clocks, deployed migration
head, PG extensions or native version. Those staging values are unavailable in
this baseline and must be observed at staging integration/release acceptance.
RDS metadata showed an Aurora PostgreSQL 15.17 instance named for production;
its ownership by staging was not established, so it is not assigned as staging's
PG version. No database credentials or customer rows were read.

Candidate defaults are task time limit 3,600 seconds, broker visibility 4,500,
processing expiry 14,400, and a Redis processing lock renewed every 20 seconds
with a 60-second TTL. These are source defaults, not observed staging clocks.
The candidate v1/v2 retrieval routes return JSON; an SSE design is not evidence
of an existing streamed transport.

The declared canonical results/uploads bucket is `knowhere-storage-staging`.
Its lifecycle API returned `NoSuchLifecycleConfiguration`: no bucket lifecycle
TTL was configured at inspection. This does not implement the reference-aware
indexing-input collector. Eligible failed revisions still need retained input;
KW-06 owns reclamation and KW-09 measures retained capacity.

Released SDK source was checked through authenticated GitHub access:

| SDK | Released baseline | Revision |
| --- | --- | --- |
| Node | v2.8.1 | `2bd1aaef90d5c26ad54740f74aed8b736312e996` |
| Python | v2.2.1 | `3181528efb35b1b0637925b3a89e8083ecad9c40` |

Exact jobs/polling sources and project files are hashed in the private
`released-sdk-baseline/inventory.json`. Local sibling checkouts were older
(Node 2.7.1, Python 2.0.0); they were preserved and are not the released baseline.
Released default polling still waits for overall completion, and result loading
has no delivered parsed-wait selector. This is source compatibility inventory;
actual builds, old/new API compatibility and parsed-result helpers remain KW-05.

Legacy migration/projection review confirmed additive phase fields, completed
backfill for old done jobs, parsing-failed backfill for eligible old failed jobs,
and preserved terminal notification defaults when subscriptions are omitted.
New milestone keys/snapshots do not rewrite historical outbox payloads. Existing
result delivery remains available for committed parsing results, and candidate
indexing failures preserve parsing charges. Remaining compatibility work is
explicit: early redelivery/owner fencing (KW-02), single-execution/phase-aware
settlement and transition barriers (KW-03), SDK/old outbox acceptance (KW-05),
retained-input collection (KW-06), and controlled PG repair/migration (KW-10).

## KW-02 byte-packing follow-up

A separate `development/` checkout applies only the five files listed in
`kw02-source-manifest.json`; `kw02-development.patch` applies cleanly to the
frozen baseline. `LexicalBulk` packs whole units by actual UTF-8 NDJSON bytes,
including action/source newlines. The worker reads
`KNOWHERE_OPENSEARCH_BULK_MAXIMUM_BYTES` (candidate default 4 MiB). Oversized
individual units fail explicitly; IDs and per-item/visible-count checks remain.

Five new protocol tests first failed against the fixed-200 writer, then passed.
Local lint/type checks passed, and the owned native selection passed all 15
cases, including the original native/webhook checks. The largest frozen
1,862-unit/103,025-token-occurrence document used one 1,624,262-byte bulk and
all expected record IDs were immediately visible. The bulk body size includes
this probe's build ID and is not the historical probe's identical wire body.
Its 2.68-second write observation excludes preparation/PG activation and does
not qualify the planned whole-execution deadline or staging load.

The wider follow-up recorded **65 passed, one failed, two skipped**. The failed
unchanged capacity-admission case took 6.90 seconds against a six-second
assertion and performs no bulk write. Separate frozen/new-version executions
both passed under the same final resource caps. Preserve all three outcomes:
this does not establish a cause or eliminate timing variability, and no test
assertion was relaxed. KW-03 owns deadline closure; this campaign is not an
all-green timing acceptance. `results/kw02-contracts.xml`, `timing-baseline.xml`
and `kw02-timing.xml` preserve the evidence.

Inline execution, early durable parse ownership, non-gating notifications,
removal of automatic indexing dispatch/replay and real broker integration
remain open. KW-02 is in progress.

## Cleanup and later supplements

The owner authorized removal of the old `knowhere-retrieval-lab-opensearch`
container and its exclusively referenced `knowhere-retrieval-lab-os-data` volume.
`old-native-cleanup-receipt.json` records the exact objects. Original fixtures,
borrowed dependencies, application port 18765 and the other 13 containers were
preserved. Do not run broad Docker or directory pruning.

After collection, stop the owned test node by the exact ID in
`native-identity.json`, remove that container, and create `guard-stop` to finish
the guard. The test unit's private PG filesystem disappears automatically.
Keep the private source/fixture/evidence package for handoff; it is not a running
service. `results/cleanup-receipt.json` records the observed final cleanup.

Later tasks append their source identities, commands and results at functional
closure, as required by the plan's Phase B register:

| Task | Pending delivery |
| --- | --- |
| KW-02 | Inline parse-worker execution, early durable phase/owner gate, non-gating parsed notification, real Redis/Celery/gevent boundary; candidate concurrency/prefetch and byte limits. |
| KW-03 | Single execution, shared deadline, synchronous manual retry, sweep, cancellation, transition and fault contracts. |
| KW-04 | Real retrieval/cache/auth/hydration integration and fatal agent-native errors. |
| KW-05 | SDK builds, released versions, wait/load/retry and webhook compatibility acceptance. |
| KW-06 | Lifecycle/maintenance fairness, input collector and retention/race procedures. |
| KW-07 | Approved target infrastructure, TLS/auth, repositories and backups; cost approval before billed work. |
| KW-08 | Native recovery, alert delivery and measured restoration drills. |
| KW-09 | Corrected evaluator, unchanged baseline/extended suite and final staging mixed-load qualification. |
| KW-10 | Compatible PG primary path, backfill and rollback reconciliation. |
| KW-11 | Release/abort rehearsal, deployment identities and go/no-go evidence. |
| KW-12 | Post-launch observation; legacy retirement remains a separate approval. |
