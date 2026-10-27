# Progressive ingestion implementation and verification

Overall requirement status: in progress and under continued discussion as of
2026-10-27. This report records an earlier implemented and locally verified
subset; it does not establish final product acceptance or deployed-client
acceptance. The [development plan discussion checkpoint](progressive-ingestion-development-plan.md#discussion-checkpoint-first-snapshot-reading-2026-10-27)
records the latest List/Tree decisions and the user's explicit unfinished status.
Subsequent capacity evidence is in the [mixed-workload follow-up](progressive-mixed-capacity-validation.md).
The original implementation described below was not pushed, published, merged,
or deployed at the time of that report.

The accepted 2026-10-27 [citation navigation amendment](../adr/0022-open-citations-on-the-enhanced-original-page.md)
is implemented and locally verified by the subsequent Hipo/SDK follow-up.
Existing citations default to the committed enhanced reader's same original-file
page; retained snapshots are explicit provenance views. The historical
snapshot-pinning behavior and test counts below predate that follow-up. Its
current evidence is recorded in Hipo's
`docs/qa/progressive-citation-2026-10-27.md`; SDK publication and deployed-client
acceptance remain pending.

## Delivery locations

| Repository | Branch | Implementation commits |
| --- | --- | --- |
| Knowhere | `feat/wangbinqi/progressive-ingestion` | `ff162946`, `2d07a2ca`, `851da937`, plus the final concurrency follow-up |
| Knowhere Node SDK | `feat/wangbinqi/progressive-snapshots` | `2fcba08`, `2bba10f` |
| Hipo | `feat/wangbinqi/progressive-ingestion` | `c5bd0980` |

The Knowhere checkout is `/Users/ontosai/.codex/worktrees/progressive-parse-plan/knowhere`.
The SDK checkout is `/Users/ontosai/github.com/ontosai/knowhere-node-sdk-progressive-20261026`.
The Hipo checkout is `/Users/ontosai/github.com/ontosai/hipo-progressive-ingestion-20261026`.
Original checkouts were preserved. Planning is committed separately as `e9513bf1`.

## Implemented behavior

- Follow-up decision on 2026-10-26: newly admitted progressive revisions keep
  their baseline live until final publication. Structure only schedules work;
  completed scopes and repaired vectors are stored as immutable intermediate
  outputs. Finalization downloads committed outputs, uploads the complete package,
  then replaces live content/index and admits the snapshot/outbox event in one
  database transaction. A failure preserves the baseline and reuses completed
  work on retry. Existing in-flight revisions retain their original protocol.
  No SDK/Hipo wire change or new public selector is required. BM25, visual RRF
  fusion and Agent retrieval algorithms are unchanged.
- Final publication advances the namespace serving generation together with the
  live content/index and namespace snapshot. Uncached classic and Agent retrieval
  validate that generation before returning; a concurrent change retries once,
  while repeated changes return an explicit conflict. Forced PostgreSQL races
  now pass. Production deployment and capacity acceptance remain separate gates;
  see [final publication validation](progressive-final-publication-validation.md).
- Initial completion publishes an immutable readable baseline. The business Job
  stays done while independent structure, scope, vector repair and finalization
  tasks continue. No additional public ingestion selector or rollout framework
  was introduced.
- Queued dispatch gaps, expired claims, bounded retries and final upload failure
  recover through the existing worker and sweep. Publication checks the active
  revision and claim generation. Stale workers write distinct artifact keys;
  a fenced transaction selects the structure input mapping.
- Repair preserves successful vectors and stages newly repaired vectors until
  final publication for new revisions (legacy in-flight revisions still upsert).
  Final completion requires structure/scopes, recorded missing vectors, and an
  uploaded final archive. Descriptor and final webhook outbox event commit
  together. Retry leaves ingestion settlement and completed work intact.
- API status exposes enhancement, retained snapshot descriptors and known page
  count. Full results omit absent enhancement fields. Both success webhooks keep
  the existing `completed` status spelling; delayed initial delivery retains its
  baseline identity even after final publication.
- Required query embedding errors fail classic and both agent retrieval routes.
  Cursor recall deadlines also fail the request while required recall is still
  outstanding; budget expiry cannot silently turn that into partial success.
  Ordinary invalid tool arguments remain recoverable.
- SDK storage separates upstream Job Result identity from reader snapshot
  identity. It preserves retained reads, independent checkpoints and a current
  pointer advanced after commit. Copied entries and retained archives are bound
  to SHA-256 verified compressed bytes, including Range and resumed transfers.
- Hipo tracks upstream enhancement and local publication independently. It
  handles duplicate/out-of-order events, retries the failed stage, keeps baseline
  reading/Ask available, and reuses bounded server recovery plus visible-client
  local status checks. Status recovery reuses known page count without loading
  result ZIPs.
- Reader baseline Tree uses ordered page leaves, including native pages without
  images. Final refresh preserves mode and source page. Resource capabilities,
  historical notices and source-PDF citation preview follow the actual read
  snapshot, including citations surviving a later reparse. Latest-version
  navigation is explicit and preserves cited content if the replacement fails.

New Knowhere migrations are `9c0d1e2f3a4b` (scope claim generation) and
`ad1e2f3a4b5c` (pending enhancement recovery index). The latter supports both
concurrent standalone DDL and the repository's caller-owned transaction path.
Hipo adds `0081_progressive-ingestion.sql` for independent enhancement state,
recovery timestamps and pending-work indexing. Existing migrations were preserved.

## Verification evidence

| Scope | Result |
| --- | --- |
| Knowhere shared lifecycle/delivery/retry/source-PDF/embedding/harness tests | 56 passed |
| Knowhere API publication/retry/webhook/QStash/concurrency/router contracts | 38 passed after the final concurrency follow-up |
| Knowhere worker Phase A/scope/reliability/retrieval contracts | 60 passed after the final-publication follow-up |
| Knowhere Ruff and Pyright (`make check`) | Passed |
| SDK complete suite | 400 passed across 22 suites |
| SDK lint, typecheck, build and changeset checks | Passed; three existing lint warnings |
| Hipo complete unit suite | 2,268 passed across 292 files; six existing skips |
| Hipo fresh PostgreSQL boundary suite | 32 passed |
| Hipo Chromium reader suite | Five passed across three files |
| Hipo typecheck and lint | Passed |

The final-publication follow-up reran 29 selected shared lifecycle/delivery/
retry/source-PDF/embedding checks, 21 API contracts and the 60 Worker contracts,
plus `make check`. Eight added/expanded Worker cases cover private structure
scheduling, baseline retention while scopes finish, staged partial vector repair,
final upload interruption, database rollback and recovery without rerunning
successful scopes. They use real local PostgreSQL and mocked external providers/
object storage. SDK/Hipo checks above belong to the preceding implementation;
those repositories were not modified by this follow-up.

The subsequent final concurrency/capacity investigation reran 47 selected shared
tests, 38 API contracts and 60 Worker contracts, plus `make check` and explicit
Pyright validation of the new concurrency contract. Its quota-limited Linux
publication-core campaign completed 427 uncached queries successfully, including
six generation-triggered retries. Production readiness is still blocked; see the
separate validation report for resource measurements and evidence boundaries.

Knowhere persistence tests ran against local PostgreSQL 16 with pgvector, with
additional SQLite contracts for small failure cases. Hipo applied its complete
migration chain to an isolated local database. Its PostgreSQL server was stopped
after testing; its data directory was retained. No production database was used.

Behavior regressions were observed failing before implementation for dispatch
recovery, stale claims/artifact writes, vector preservation, upload recovery,
snapshot/checksum identity, webhook ordering, retry state and reader transitions.
Independent reviews found the stale storage-write race, SDK verification and
pointer gaps, Hipo polling transfer amplification, and historical filter state;
those findings received fixes and regression coverage. One pre-existing harness
prompt assertion used obsolete wording; its expectation was updated to the
unchanged current same-turn dependency rule.

## SDK installation gate

Local Hipo acceptance used the packed SDK from `2bba10f`:

```text
SHA-256: 17c2b694b829af7e1e2a2a65b1de69c2779efc36e8a6b3bb61d0dc1d95b3a886
```

Hipo's checked-in manifest and lockfile retain the published SDK 2.5.0 dependency;
they contain no machine-specific tarball path. That published runtime does not
contain these progressive semantics. Publish the compatible SDK through its
existing minor-release changeset, pin the published artifact in Hipo, and rerun
the synchronization/reader acceptance before merging or deploying Hipo. See
Hipo's `docs/progressive-ingestion-release.md` for the local installation evidence.

## Remaining release validation

1. Restore healthy deployment configuration, then verify provider access,
   migrations and halfvec support against the actual target environment. The
   2026-10-27 read-only inspection found a missing `VERCEL_KEY` secret field and
   only one of two desired Worker tasks running; see the final validation report.
2. Publish/pin the compatible SDK and repeat Hipo acceptance against that exact
   installed release.
3. Exercise real native PDF, scanned PDF and PPTX ingestion, early Ask visual
   citations, two-event delivery, browser-closed recovery, and final publication
   across deployed workers/services. Local mocked-provider/component tests do
   not establish this outcome.
4. Measure full/progressive ingestion and simultaneous retrieval on representative
   and large documents, including new admissions after earlier Jobs reach M0.
   Observe CPU/RSS/disk, queue delay, DB locks/connections, provider concurrency,
   retries, download amplification and end-to-end latency.
   The multi-statement/Agent race across final same-JobResult replacement was
   reproduced and fixed with local PostgreSQL contracts. Repeat it against
   deployed workers/services; isolated publication measurements do not establish
   full ingestion capacity or production latency.

Immutable baseline/final archives, source PDFs and retained Hipo objects increase
storage and transfer. Processing uses separate mutable input storage and immutable
claim-specific outputs. SDK checksum verification uses temporary disk proportional
to compressed archive size, including the progressive Range path. These costs
must be included in capacity acceptance for both workers and the Hipo runtime;
green local tests do not prove the current server configuration is sufficient.
