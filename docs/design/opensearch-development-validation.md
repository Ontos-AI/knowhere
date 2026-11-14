# OpenSearch development validation

Development closeout, 2026-11-05. The
[approved plan](opensearch-development-plan.md) remains the acceptance contract.
The original [KW-01 handoff](opensearch-kw01-handoff.md), frozen source, input
labels, historical failures and private receipts are retained unchanged.

The worktree contains the application, both SDK integrations and the executable
staging, recovery, workload and release tools. Local tests, loopback service
tests and isolated Oracle checks qualify different parts of that work. Formal
integration and the target load, durability, recovery and release gates require
the reviewed staging environment. No production deployment, SDK publication,
paid parser/model campaign or new billed AWS operation is included.

## Final source and campaign record

| Record | Development evidence |
| --- | --- |
| Reviewed worktree/archive, branch and base | `source-v10`, 1,358 source/configuration files on `feat/wangbinqi/opensearch-publication-slice`, base `fc186b185137dcf25d9d5b073dac49f1c895cacb`. Archive SHA-256 `af2cffb958f85c16d27ab5ea65dc3180a879616c9e67c0a03aa5949b53c5fe84`; includes the relevant dirty and untracked candidate. |
| Aggregate runtime/operator source hash and file manifest SHA | File manifest SHA-256 `ce8de0ebcc7ffd68595bb37ac1f454b20e69290904267b16bc4c73d81b6c8061`. Both extracted staging contexts produce `19772710ac8836e8dbb31c3406a124167d1eaaec49c298db60dd711aa7520e8d` with the local ARM64/syntok runtime; the effective deployed compiler can differ. |
| Dependency, compiler/tokenizer, API/worker and SDK artifact identities | Frozen `uv.lock` SHA-256 `19833d58aeb7e657caf0d5439dac3a7c7fe493dc7d081db6fed6e4bb07302c30` and sole Alembic head `8f901a2b3c4d`; API/worker Linux amd64 contexts from the recorded KW-01 base digests, receipts `5a9f47857b092c4978238b2db13d27dcc25c9af9d7f1b71c7966ba2eb54a8d3c` / `9d38a5018893a5c7e8ead0cdcc94d935bba00a93f680ed9f40f1456851a4d65d`. Node/Python source and build manifest `faa6198e74940be3a302ef065b24d793a49b15028c04724aa42492acd075c6c4`. No built or deployed staging image is claimed. |
| Oracle development and preservation | Private root `<lab-home>/knowhere-kw01-development-20261105-01a1ac05`, final `source-v10`, PG 14.18, Python 3.12.13, pinned OpenSearch 3.9.0/Lucene 10.5.1. The Oracle runtime hash matches both local contexts; effective compiler SHA-256 `ecad588bc00f61814663992031ce04c98fdebe940b2d66862ccf6183125574e7` (syntok). Final teardown removed the owned native node and temporary storage; test and guard units are inactive. All 13 comparison container IDs, start times and pause states matched, HTTP 200. All 23 business and four scale fixture files matched both original and frozen copies. |
| Local API/shared/worker and tooling contracts | 733 API and migration passes (51 opt-in skips), 369 worker passes, 546 shared and tooling passes (three native opt-in skips). Ruff, Pyright and diff whitespace passed. Manifest `final-local-v7/manifest.json`, SHA-256 `7d7d5bf923b475f4e69fcc4c9852802727caa47720807d57687175134488fa5c`. Three real SDK HTTP cases passed, manifest SHA-256 `cd781675927e2f7e7bc0f4abaf1f3b95d3f7b8b6600b465e8274457b39658b26`. |
| Original parse-task/Redis/Celery/gevent | Source-v10 on the original task handler: success, write failure and actual ten-second timeout; duplicates did not reparse, re-execute or enqueue indexing. Parsed result and charge stayed intact. Probe result `worker-20261105T210802Z-artifacts/receipt.json`, SHA-256 `7c112806ff4d7bd6ae6ddcc4152bdf8c176a595b7fa6ae608f35c5816655195b`; concurrency 2, prefetch 1, prepared parser seam. This is separate from the staged prepared-completion workload bridge. |
| Unchanged 20-query baseline | Source-v10 precompiled mode passed all 160 comparisons across eight stages: 112 shared successes, 56 shared misses, zero lost PG-found groups and zero identity failures. Report SHA-256 `4e974eaebbf5333aafbfb0a961d31ac9c2bef1ad46d687a1cc9e8e452c0d0fa9`; runner exit receipt SHA-256 `054b2afc3af2b94be8e44b972e2bc60e1b0df11c3f3249373a2b7678d475165c`. The historical v4 raw diagnostic also completed with the same counts; the final production-code raw attempt at D415 did not qualify the full deadline. |
| Separate automated-derived filter replay | Source-v10 precompiled mode completed 40 queries and 320 stage comparisons. The gate failed: 128 shared successes, 37 shared misses, three PG-only observations of the same evidence group after activation, zero identity failures. Report SHA-256 `99a42ac7c0371d4a84f0322e69f85b49815c86a6ec68e54f4a1bad2437a9fdaa`; exit receipt SHA-256 `95636e384d2541ed50bbb60413901a8befbb5e01f58b14bab98cdfce340a70cc`. The original case and labels remain in the suite. |
| CLOSED full-FS snapshot/restore probe result and identity map | Owned OpenSearch 3.9.0, `closed-20261105T195346Z.private.json` SHA-256 `42695aea1fc577b67aecc8c1d6475b5c39d6b42079f3ac866bae51c79dbad8f3`: one OPEN and one CLOSED source, both shards verified, cold source never opened for capture, fresh restored physical UUIDs, exact payload verified and owned artifacts removed. FS test only; no independent S3 evidence. |
| Oracle native and capacity contracts | 28 native, 67 integrated and seven focused execution tests passed; the opt-in full capacity case passed separately on real indexes, including the 401st admission. Receipts: `native-20261105T205107Z-receipt.json`, `contracts-20261105T205303Z-receipt.json`, `slots-20261105T210053Z-receipt.json`. Earlier v3 scale verified 42,794 real units, 644 documents and 20 depth queries; 2,206 derived rows exercise the 45,000-unit boundary without joining the published scope. |
| Retained large input | `retained-20261105T205929Z.private.json` SHA-256 `b152d9c85c62de61c5edcd90fdb936f9fbd9c69bdaf6c05c9b8a6838a968a220`: exact 1,862 units and 103,025 token occurrences, one 1,652,192-byte request, traced peak 17,653,370 bytes and RSS high-water 80,162,816 bytes. This is retained-token selection, deserialization and packing, not raw compilation or a full attempt. The exact corresponding raw record was not in the retained package. |
| Native/staging configuration and remaining deviations | The isolated Oracle node used a filesystem snapshot repository, disposable data, loopback and test-only auth assumptions. Effective staging durability, security-plugin role access, mixed load and traffic/rollback acceptance remain unqualified. |

The broad local and Oracle native suites ran on `source-v7`. Its complete
per-file comparison to `source-v10` changes only three contract fixtures:
the async request synchronization watchdog, the real worker broker startup
and teardown, and the test-only precompiled input assignment. Production
application, operator and dependency bytes are identical. The comparison
receipt is `source-v10-test-only-delta.json`, SHA-256
`d5a8bc81e71b78b9e939291092bd7aedb0bc4842d9a8265f4bd2c9c437146eb0`.
The affected probes are run separately with the final fixture bytes.

The private Oracle evidence archive and its per-file manifest are retained at
the Oracle development root and in the local `opensearch-development` artifact
directory. The archive contains 286 verified entries; SHA-256
`42d2a3e060d0f1960f6ff2cd51ed04fe362824e305ac2cf5608af307ee635487`;
manifest SHA-256
`5ac7af7e83ab845f846f086cbfae46684ce651f1e7d2cf2387026f47b9c44381`.
The 2,617 guard samples recorded zero trips and zero failed comparison probes;
minimum available memory was 4,412,059,648 bytes and minimum free disk was
3,843,710,976 bytes. No task-owned Oracle container remained after teardown.

Private document content, query text, evidence labels, prepared payloads, tokens
and raw reports stay in the private evidence package. Record paths and checksums
here without copying those payloads into Git. A skipped, interrupted or failed
campaign remains visible and cannot supply a passing acceptance receipt.

## Delivered software and verification boundaries

| Task | Delivered implementation | Verification and remaining boundary |
| --- | --- | --- |
| KW-02 | Parsing ownership before expensive work; persisted parsed result/milestone before inline indexing; one initial execution in the existing parse task; byte-bounded UTF-8 NDJSON with `refresh=wait_for`; non-gating webhook submission. | Execution, parsing ownership, lifecycle and native bulk contracts. The real original-handler broker probe is separate from the prepared completion bridge and requires its own final receipt. Full parser/provider throughput is outside these prepared-input tests. |
| KW-03 | Durable attempt origin/owner/start/deadlines; one execution per admitted attempt; synchronous free manual HTTP retry; phase-aware abandoned-attempt settlement in the existing stale-job sweep; retained parse/result/charge on indexing failure. | Connection/bulk/timeout, duplicate delivery, old-owner, admission, cancellation/response-loss, settlement and completion-race contracts. Effective staging gateway, broker, worker and sweep timing remain deployment evidence. |
| KW-04 | Version-bound async native retrieval; private/demo authorization; existing API runtime/lifespan and bounded recency flushing; guarded CLOSED recovery; fatal native failure propagation through both agent harnesses and tool wrappers; canonical unavailable errors. | Async publication/read/recovery, API lifespan, authorization/cache/hydration and fatal agent failure contracts. Fake agent/model boundaries do not qualify paid provider behavior. Normal hot reads retain the no-coordination-write/no-native-preflight contract. |
| KW-05 | Node and Python sync/async SDK wait selectors, phase/result context, free explicit indexing retry without automatic mutation resend, retained parsed-result loading; immutable subscribed webhook milestone snapshots and bounded dispatch/signing support. | SDK unit/build/type checks and real SDK-to-loopback-API contracts. Packages remain unreleased; staging SDK compatibility and package release require their separate signoff. |
| KW-06 | Fair bounded lifecycle/PG-copy/cleanup/input maintenance; immutable input identities and reference-aware collection/orphan handling; retained CLOSED empty indices with identity-preserving reopening; capacity/ownership reconciliation. | Maintenance, input-retention, turnover, 400-slot, private/demo and cleanup-race contracts. Production catalog growth, backlog, byte bounds and warning thresholds require measured staging evidence. |
| KW-07 | Reviewable private single-node CloudFormation/install bundle, retained encrypted data volume, scoped live/full-repository configuration, request durability, TLS/client authentication, secret-file settings, preflight and explicit cost manifests. | Pure configuration tests, transport/security tests and dry-run plans. Actual VPC/subnet/AMI/SG/SSM/S3/KMS identities, routes, permissions, artifact checksums, current quotes and explicit cost approval remain required before installation. |
| KW-08 | Authoritative PostgreSQL export; hourly OPEN/CLOSED full capture; independent bounded offline restore and payload verification; verified-clone cleanup and three-point retention; off-node application/backup age and native health monitor. Source-owned cross-host issuer and durable fence intent, existing-host EC2/SSM process and healthy-volume drivers, restart identity reconciliation, and guarded live-remote/full-snapshot recovery. | Local-double export→snapshot→restore/read/cleanup, process/volume/fence/reconcile and unknown outcome contracts; actual isolated PostgreSQL ownership/admission/restart tests; pinned Oracle CLOSED FS component evidence. Actual S3/SSM/PKI/role qualification, recovery times and alert delivery are separate. |
| KW-09 | Relative evaluator, unchanged baseline and separately versioned derived suites; isolated replay and completed/gate receipts. Frozen workload schema, source-owned arrival admission/preconditioning, real prepared-completion Celery bridge, correlated stage/resource export, executable snapshot/maintenance overlap and result evaluator. | Local component workflow exercises real command/file protocol with isolated broker/native doubles. The extended Oracle filter gate found one reproducible ranking loss across three stages; final quality remains open. Reviewed read/write inputs and human evidence, actual target resource measurements and the complete staging hour plus five-minute catch-up remain separate. Sampled PG lock and JVM collection metrics are reported with their limitations. |
| KW-10 | Compatible PG-primary inline publication, frozen-token PG coverage/repair, bounded non-gating PG copy, source-owned gated migration/backfill/verification/resume/abort and guarded restore admission with immutable receipts. | PG-primary, publication-gate, current-published-coverage, transition, RLS/inventory and restore-admission contracts. Actual deployment/migration/restoration campaigns and measured budgets remain separate. |
| KW-11 | Existing ALB/ECS release bridge and individual transition wrapper; physical issuer/source/receipt checks; explicit customer gate and public closure verification; API/worker deployment, source coverage and private/public phase/citation smoke; compatible PostgreSQL rollback preserving first impact and unknown-action containment. | Local-double gate/deploy/smoke, known failure and rollback, and lost gate response contracts. Real reviewed rule/task definitions, exact measured restoration reserve, staging rehearsal and production authorization remain acceptance prerequisites. |

The software defaults are candidate configuration. Current source defaults include
an OpenSearch operation budget of 10 seconds, separately bounded settlement of
2 seconds, a PG-primary budget of 300 seconds, an unstarted-attempt threshold of
300 seconds, a 4 MiB native bulk byte budget and idle watermarks of 360/320. They
are not measured staging throughput, concurrency, memory or latency limits. The
existing stale-job sweep retains its 30-minute schedule; record actual detection
and bounded-scan lag separately. Graceful gevent worker process exit drains native
background I/O before interpreter teardown; service supervision and durable
intent still cover forced termination.

## Development regressions found by the integration campaigns

- The first raw business replay rejected an input of 121,884 characters at the
  old 65,536-character work boundary. The maximum in the retained business
  inventory was 340,269. The reviewed default is 524,288; aggregate logical and
  working-set limits, the 4,096-character uninterrupted CJK span guard, and
  4 MiB bulk packing remain in force. Size failures expose a safe
  `INDEXING_INPUT_TOO_LARGE` reason through the existing error contract.
- The larger boundary exposed repeated marker scanning and scheduler overhead.
  The bounded scanner preserves the frozen tokenizer output, including
  malformed or nested marker cases; the compiler checks the deadline at every
  checkpoint and coalesces cooperative yields into roughly one-millisecond
  intervals. The three alternating synthetic controls kept all 135,928 tokens
  and the complete payload equal, cut scheduler yields from 280,860 to 318–321,
  and reduced median local compiler CPU time from 538 ms to 343 ms.
  The preceding scanner-only Oracle run timed out preparing D415 before a
  native write. The later raw diagnostic completed the complete ranking
  matrix: D415 preparation was 7.69 seconds, including 5.89 seconds of compile,
  and two whole calls returned beyond ten seconds under the Oracle CPU quota.
  These are component observations with different source manifests. The final
  production-code raw replay also reached D415. Input preparation took
  7.60 seconds; activation exceeded the remaining budget, rolled back and
  settled one failed attempt. This run reached no ranking stages and did
  not qualify raw publication on that CPU quota.
- A real PostgreSQL regression forced the monotonic deadline to expire after
  completion state and event preparation but before commit. The previous worker
  committed. The fixed transaction checks the remaining time at the commit
  boundary; it rolls back the publication pointer, completion state and event
  while retaining parsed data and charge. Committed outcomes still win a race
  with bounded settlement. Native and PG publication continue to use their own
  configured budgets.
- The prior candidate failed caller-owned clean-schema upgrades because a
  deferred demo-catalog foreign key check was still queued when later DDL
  reached the table. The Alembic environment validates that specific new foreign
  key after the seed revision and restores its deferred mode, with no commit of
  the caller's transaction and no change to existing migration files. See
  [PostgreSQL's constraint timing semantics](https://www.postgresql.org/docs/15/sql-set-constraints.html).
- The staging overlay derives its Docker platform from the exact reviewed ECS
  task definition in the source archive. The recorded staging API and worker
  task definitions both require Linux x86-64. Each context binds its reviewed
  base digest and verifies every archived file; the context receipts do not
  claim an image build or a deployment.
- The Oracle real-broker fixture could miss Redis readiness before its old
  five-second setup window under the test cgroup. The failed run left an owned
  container until its separately verified cleanup. The final fixture bounds
  setup separately, keeps the production ten-second deadline, and scopes
  shutdown to the exact owned broker label and name. A separate async contract
  sync-point watchdog uses the actual request budget with the API's compiler
  warm-up, and reports an earlier task failure directly.
- The first precompiled replay locked an ORM result and its nullable joined
  job together, which PostgreSQL refused. The test fixture now locks the result
  row only, as the product assignment does; the raw replay path is unaffected.
- Two attempts to use the cached local Docker OpenSearch image did not reach
  readiness (an owned-container OOM and a startup inspection timeout). Both
  owned containers were removed. No native or ranking result is assigned to
  those attempts; the Oracle results above use the isolated Oracle node.

## Local regression evidence

The operator/evaluator/client/workload selection below is pure, filesystem-only
or uses injected HTTP. It creates no PG/native service and can run independently
of the serial Oracle campaign:

```bash
uv run --offline --frozen python -m pytest -q \
  deploy/opensearch/tests \
  scripts/probes/tests/test_opensearch_ranking_support.py \
  scripts/probes/tests/test_opensearch_ranking_command.py \
  scripts/probes/tests/test_opensearch_client_security.py \
  scripts/probes/tests/test_opensearch_workload.py \
  scripts/probes/tests/test_opensearch_workload_command.py \
  scripts/probes/tests/test_opensearch_workload_execution.py \
  scripts/probes/tests/test_opensearch_closed_snapshot.py
```

The final local suite also includes every shared contract and each probe test.
The actual commands, pass and skip counts, and file identities are recorded in
the campaign manifest. Native and broker opt-in skips in the local suites are
accounted for separately by the Oracle campaign.
Focused Ruff and Pyright checks passed for the delivered operator, replay, workload,
prepared-completion and native client modules. These checks do not replace the
database/native/broker selections in the final campaign record.

Regression development retained failing-before and passing-after evidence for:

- A lost PG-found evidence group, shared misses and independent identity errors.
- Read-only baseline derivation, explicit baseline/extended identities and a
  skipped pytest run lacking a completed matching quality report.
- Invalid/public client endpoints, missing trust/authentication, secret permissions,
  bounded credential generations and explicit runtime shutdown.
- Wrong/missing snapshot metadata, missing CLOSED catalog/shard coverage, hourly
  submission cadence and preservation of three complete verified points.
- Unknown restore/close issuer outcomes, wrong native target identity, source
  mutation authorization before dispatch and rejection of OpenSearch under the
  weaker PG rollback policy.
- Malformed monitor resource metrics, producer queue overflow/cancellation,
  insufficient latency samples, failed catch-up work, visible verified exclusions
  and exact campaign/source/event telemetry correlation.

The SDK owner recorded 608 passing Node tests across 29 files with SDK/MCP builds
and type checks, and 308 passing Python tests with wheel/sdist and extracted-wheel
validation. Node lint retains ten pre-existing warnings. Their worktrees remain on
`feat/wangbinqi/opensearch-phase-semantics`. The phase changes remain unreleased;
the unchanged package version fields are Node 2.8.1 and Python 2.2.1. The real SDK-to-loopback-API selection recorded three
passing Node/Python-sync/Python-async cases across 200/409/500/503/504 outcomes,
immutable callbacks, and no automatic resend after failed/lost mutation replies.
Its native/auth/artifact/callback boundaries are synthetic. The private manifest
is `opensearch-development/sdk-http/completed-development-run/manifest.json`,
SHA-256 `1d26c99ef31f1828eff8590ec630a877639e9dce8c7ced85b1204155a0181149`.
The source-v7 rerun binds the same SDK artifacts to the final API changes.
The SDK packages have not been published.

Reproduce the Node selection in its isolated SDK checkout with
`pnpm run test:all`, `pnpm run build:all`, `pnpm run typecheck:all`,
`pnpm run lint:all` and `pnpm run format:check:all`. Python's recorded result used
the built/extracted wheel with isolated pytest/respx dependencies and the cached
Python 3.12 interpreter; preserve that wheel/dependency receipt instead of treating
a stalled workspace dependency sync as successful verification. The real SDK/API
wrapper is `scripts/probes/verify-sdk-http-contract.py`, with explicit
`--node-sdk-root`, `--python-sdk-root` and a new `--artifact-dir`.

The SDK owner also recorded eighteen actual TLS transport tests in
`scripts/probes/tests/test_opensearch_tls_transport.py`, covering the sync/async
clients against an owned loopback TLS responder. This adds handshake/authentication
and bounded-timeout evidence. The responder is not OpenSearch; actual native
security-plugin roles, private deployment access and credential rotation still
need the target-environment receipt.

## Separate baseline and automated-derived replay

Run from the reviewed development checkout. Oracle's borrowed Python and private
fixture root are explicit; neither the frozen KW-01 source nor the fixture files
are rewritten:

```bash
FIXTURE_ROOT=<lab-home>/knowhere-kw01-20261105-01a1ac05
DEV_ROOT=<lab-home>/knowhere-kw01-development-20261105-01a1ac05
ORACLE_PYTHON=<lab-home>/knowhere-readfree-r8k2/.venv/bin/python
OPENSEARCH_ENDPOINT=$("$ORACLE_PYTHON" -c \
  'import json,sys; from pathlib import Path; print(json.loads(Path(sys.argv[1]).read_bytes())["endpoint"])' \
  "$DEV_ROOT/native-identity.json")
cd "$DEV_ROOT/source"
mkdir -m 700 "$DEV_ROOT/results/extended-inputs-v1"

"$ORACLE_PYTHON" scripts/probes/replay-opensearch-ranking.py derive \
  --baseline "$FIXTURE_ROOT/fixtures/business/manifest.json" \
  --output "$DEV_ROOT/results/extended-inputs-v1/manifest.json" \
  --suite-id business-automated-filters --suite-version 1

"$ORACLE_PYTHON" scripts/probes/replay-opensearch-ranking.py run \
  --manifest "$DEV_ROOT/results/extended-inputs-v1/manifest.json" \
  --output "$DEV_ROOT/results/development-ranking-extended-automated-v1.json" \
  --suite extended --endpoint "$OPENSEARCH_ENDPOINT" \
  --python-executable "$ORACLE_PYTHON" \
  --runtime-directory "$DEV_ROOT/results/runtime-ranking-extended-automated-v1"
```

The original `FIXTURE_ROOT`, its `fixtures` and `results`, and the frozen source
remain untouched. Every new derived manifest, result and runtime path above is
under the separate `DEV_ROOT/results`. The current owned identity reports port
32787; the commands read the receipt rather than freezing that port into the
example. The second command is a dry-run plan. The integration owner executes the
generated test command serially under the existing resource guard, or adds
`--execute` in that approved environment with the isolated installed `pg_ctl`
configured. Never run another contract server on the shared test storage root.
The derived input directory is immutable and separate from the report/runtime
directories; the ranking harness rejects results written beside their manifest.

The derived suite always contains include-marked and exclude-all variants.
It also adds cross-namespace variants when the reviewed manifest has a foreign
document. The actual business fixture has a single namespace, so this run
contains forty cases. The two-namespace tool regression exercises the sixty-case
derivation; it does not add queries to this business run. The new manifest
binds the baseline SHA, records
`provenance:"automated_derived"`, `reviewer:null` and
`human_relevance_reviewed:false`, and points to the unchanged retained chunk files.
It supplies structural/filter coverage; it does not invent broader reviewed
relevance labels. The original baseline manifest SHA in KW-01 is
`445f541447028a07ac39b82b4df578384fdfdfcde48a88f5bd541b2e98f4c421`.
Verify the current private files against the frozen inventory before and after
both campaigns.

Run the baseline separately with `--suite baseline` and its original manifest;
write its new report and runtime only under `DEV_ROOT/results` as well.
Use `--input-mode raw` (the default) to compile canonical persisted rows inside
the attempt. The explicit `--input-mode precompiled` variant builds an immutable
input from the same run's persisted rows before the attempt. It exercises the
same ten-second load/write/visibility/activation path and tests ranking and
revision transitions. It cannot qualify raw compilation within that budget.
No retained token file is rebound to a new revision or section by path.

Each v2 replay report contains its suite, provenance, manifest/input hashes,
input mode, compilation scope,
`completed` and `quality_gate`. The command manifest and exit receipt preserve
the isolated paths, pytest outcome, matching report identity, report SHA and final
gate. A pytest exit code of zero without the matching completed passing report
is a failure. Offline `evaluate --report FILE --output NEW_FILE` preserves the
incomplete-run guard.

Every PG-found evidence group must remain available at the same query/stage.
Shared misses stay visible and do not fail that relative comparison. Permission,
content, document/revision, asset and citation identity remain independent gates.
The final precompiled baseline's slowest whole attempt returned in 4.50 seconds
under the isolated caps. The strict final-production-code raw replay ended at
D415 with `DEADLINE_EXCEEDED` and one execution; report SHA-256
`26c98067a009ab443e4f10119ac349e292c51b85948630ea3d8ecce28dfde2bd`.
It preserved the deadline fence and is not a ranking or a raw throughput pass.
The earlier completed raw diagnostic is retained separately, report SHA-256
`62f9e5f3b81b28ef13fbecbb4ad7d2a4570654ef6fcbe4bafaef2f7fd2addb8a`.

The extended failure is `Q012--include-marked` with the replacement published:
PG found the evidence group at rank 9; OpenSearch did not return it in the
unchanged top 10 before, during or after cleanup. The independent identity
gate stayed clear. These are three stage observations of one query/evidence
regression, not three distinct queries. The report is complete and failed;
it cannot qualify final replacement quality.

The follow-up recreated the same owned corpus, write failures, publication
and cleanup, running only this query at the final stage. With final top 10
fixed, candidate depths 20, 100 and 200 all missed the group. A diagnostic
top-100 projection at depth 200 returned its marked chunks at ranks 19 and 90.
The evidence is retrievable; increasing candidate depth alone did not resolve
the ranking loss. The diagnostic is not a passing acceptance run (report
SHA-256 `e0b8b46f7d1707b8d8cd20fbb1a0aa3771e56d52ef8839f7c560ae8301396960`).
The next ranking comparison must preserve top 10, use versioned scoring and
reviewed cases on the target staging configuration, and rerun both quality
suites without relabeling or removing this case.
The historical `automatic-retry-visible` stage maps to
`manual-retry-visible` under the new one-execution/explicit-admission policy.
Original historical reports and labels remain frozen.

## Workload, durability and release acceptance gates

The [workload usage guide](../../scripts/probes/opensearch-workload-usage.md)
describes frozen inputs, exact transformations/cohort minima, actual deployment
bindings, the real Celery prepared-completion include, open-loop execution,
cancellation and offline telemetry reevaluation. Automated simulation explicitly
reports `actual_staging_campaign:false` and cannot qualify the target gates.
Missing actual post-parse arrival/size/dedup/admission evidence, the retained
1,862-unit case, human labels, cohort preconditions or source-bound resource
measurements remain input gates. A failing/cancelled cohort or failed indexing job
cannot disappear from the report or count as drained backlog.

The [operator guide](../../deploy/opensearch/README.md) supplies installation,
TLS/authentication, preflight/cost, backup, recovery, alert and release commands.
All mutation commands default to dry-run. Hourly full backups explicitly include
OPEN and CLOSED indices and bind snapshot metadata, UUID/catalog, every primary
shard and independently verified document/revision/build/update/delete boundaries.
Retention protects at least three complete verified points even when older than
seven days. The pinned 3.9.0 upstream CLOSED-index FS snapshot test and the bounded
local FS probe establish their own narrow behavior; neither proves an actual
independent S3 backup or restore objective.

Restore admission uses source-owned exact-epoch/RLS/current-build checks and
authoritative union capacity at or below 400. Snapshot capture UUID stability and
explicitly attested restored UUID adoption are distinct. Live-remote recovery
binds reviewed existing remote metadata, including namespaces newer than the last
snapshot. Unknown issuer outcomes retain ownership/charges and prevent opposite
operations until reconciliation. The actual native client checks its target
cluster/incarnation before each mutation; transition wrappers require the reviewed
aggregate source hash inside the mutating process before dispatch.

The isolated Oracle limits were 0.20 CPU, 1,400 MiB and 192 tasks for the test
unit; 0.30 CPU, 1,700 MiB and 768 MiB JVM heap for OpenSearch; and separately
limited worker and broker containers. The unit and native container also used
read/write and IOPS caps. A separate guard stopped only owned work on low host
memory/disk or repeated failure of the comparison endpoint. These settings
protect the shared host and are not a sizing recommendation.

The remaining target-environment evidence is explicit: private cloud identifiers
and current cost approval; actual TLS/RLS/IAM/SSM and repository access; complete
hourly recovery points; process/volume/live-remote/independent-full-restore coverage
and measured end-to-end RTO; off-node outage and backup-age alert delivery plus
operator response; the approved customer-traffic gate across SQL resume, matching
deployment and API/citation smoke; measured restoration reserve and two-release
rehearsal. Production cutover, observation and SDK publication follow their own
authorization and go/no-go records.


## Rebuildable staging contexts

The source archive and its per-file inventory are private development artifacts.
To reproduce each verified context, use the respective API/worker base image
digest from the staging inventory and a new output directory:

```bash
python deploy/opensearch/staging_image.py \
  --archive "$REVIEWED_ARCHIVE" --archive-sha256 "$REVIEWED_ARCHIVE_SHA256" \
  --manifest "$REVIEWED_MANIFEST" --manifest-sha256 "$REVIEWED_MANIFEST_SHA256" \
  --service "$SERVICE" --base-image "$REVIEWED_PINNED_BASE_IMAGE" \
  --output "$NEW_BUILD_CONTEXT"
```

The receipt renders the platform-specific build command. Run a build or use the
existing source image pipeline only in the reviewed staging release workflow.
Pin the actual built images and verify their effective aggregate source and
compiler identity before using a mutating transition command. The local contexts
inherit the recorded base entrypoint and health checks; rendering them never
pulls the base image or deploys anything.

## Post-cutover observation and conditional retirement

Use the first fourteen days to review actual query/citation quality, latency
and error cohorts, pending indexing and copy age, LRU and unresolved capacity,
catalog/disk growth, repository coverage and costs. Retain actual incidents and
recovery verification with the observed deployment identity. The review records
one decision: continue, extend observation with concrete follow-up, or run the
reviewed PostgreSQL rollback.

The temporary PG lexical copy remains active unless retirement is separately
approved. A later retirement proposal must name the exact affected legacy
lexical tables/indexes and copies, demonstrate native recovery independently of
them, stop new copy admission and drain or fence existing work before any
deletion, and preserve the canonical PostgreSQL document/section/chunk,
version, authorization, result and citation data. It must also account for
retained input consumers and live/full snapshot retention. There is no automatic
day-14 deletion.

## Q012 document-filter ranking follow-up

The isolated trace compared the marked paragraph and the one paragraph unique
to the native top ten. The native path ranks were 41 and 3, the native content
ranks were 32 and 20, and the fused ranks were 19 and 3. The corresponding
PostgreSQL content ranks were 12 and 37. The marked paragraph matched five of
six distinct query terms. The competing paragraph matched three and repeated
two of them 25 times each. A separate native index with the same 158 eligible
units and the same mapping raised the marked paragraph to fused rank 14.
Changing the corpus statistics accounted for some of the gap. The marked
paragraph was already behind the competing paragraph before result assembly.

Two PostgreSQL rebuilds produced identical channel score distributions.
The marked paragraph's tied path score landed at rank 43 in one rebuild and
35 in the other because the tie uses a generated row identity. The final
PostgreSQL ranks were 11 and 9. The follow-up regression therefore checks the
original marked group directly at each activated stage.

The async native classic route with an explicit document include filter now
adds a constant score of 1.5 for each distinct query term matched in the content
channel, for queries with 2 to 16 distinct terms. It retains OR eligibility, the
published build filters, the existing path score and fusion, and the original
result and candidate windows. A cache profile version separates the new
ranking results from earlier cached results. The profile was tested against
the frozen suites using the actual native search and API route.

The original 20 queries passed 160 query-stage comparisons, preserving 112
historically found evidence groups. The 40 derived queries passed 320
comparisons and preserved all 131 historically found groups, including the
three earlier Q012 losses. Q012 ranked seventh in all three activated
stages. Both suites recorded zero PostgreSQL-only groups and zero identity
failures. The first owned node reached its time limit during the last read.
A fresh owned node replayed the after-cleanup stage with identical source
bytes and fixture hashes. The reports retain both runs and their separate
exit statuses.

The API contract reproduced the preference for repeated terms before the
change and passed after the change. Ruff and Pyright passed. The native
nodes each had a 0.25 CPU and 1,700 MiB limit, with PostgreSQL and replay
execution on the developer machine. Both nodes were removed. All 13
comparison containers retained their identities, start times and pause states.
The 316 health probes returned HTTP 200.

## Staging candidate follow-up

The owner confirmed that retrieval quality is a migration regression check.
The current ranking profile remains selected. The exploratory T08 case is
recorded as an isolated difference. Substituting PG term weights recovered
T08 but displaced two previously marked direct passages in the broader replay.
The experiment did not change application scoring.

An isolated local OpenSearch 3.9.0 node and PostgreSQL 15 ran the original
20-query suite from raw persisted inputs. The run passed all 160 query-stage
comparisons, with 112 shared evidence hits, 56 shared misses, zero PG-only
groups and zero identity failures. D415, which timed out in the earlier capped
Oracle run, finished indexing locally in 1.43 seconds. The slowest of 22 measured
attempts took 1.59 seconds against the unchanged ten-second budget. This was
a local serial run with test storage and no target mixed load or remote
durability. Report SHA-256
`38eae237611ad019fd153332215c7c227cc7343c7068d6f51de483068a323e62`.

The focused API integration suite passed 113 cases with real local PostgreSQL
and OpenSearch. The first run exposed a test race between the demo rollback
copy submitted by the worker and the test's explicit copy under the restricted
database role. The test now runs that role-specific copy without a competing
submission. The operator and probe suites passed 199 cases.

The refreshed Linux amd64 build contexts pin the observed staging API and
worker base digests. The API context preserves bundled demo PNGs from its
pinned base alongside the reviewed source and keeps reviewed files when both
contain the same path. The generated copy step and demo catalog resolved all
985 tracked PNGs using the local fixture. The actual staging image has not
been built. Both contexts match the local aggregate source hash
`67ad447aba8470567eada10944a44cfbddfa8eb07c42d4823e7eb39f9990f01d`.

The read-only staging check found the existing API `52` and worker `55`
task definitions running without OpenSearch connection or credential bindings.
Staging acceptance still needs a reviewed private native target, effective
runtime bindings and the approved cost record for any billed provisioning.
After that, run the target mixed-load, durability, recovery and release
rehearsals under their existing gates.
