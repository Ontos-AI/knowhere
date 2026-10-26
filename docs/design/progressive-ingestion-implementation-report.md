# Progressive ingestion implementation and verification

Implementation and local automated validation are complete. Nothing was pushed,
published, merged, or deployed. This report does not establish production capacity
or deployed-client acceptance.

## Delivery locations

| Repository | Branch | Implementation commits |
| --- | --- | --- |
| Knowhere | `feat/wangbinqi/progressive-ingestion` | `ff162946`, `2d07a2ca` |
| Knowhere Node SDK | `feat/wangbinqi/progressive-snapshots` | `2fcba08`, `2bba10f` |
| Hipo | `feat/wangbinqi/progressive-ingestion` | `c5bd0980` |

The Knowhere checkout is `/Users/ontosai/.codex/worktrees/progressive-parse-plan/knowhere`.
The SDK checkout is `/Users/ontosai/github.com/ontosai/knowhere-node-sdk-progressive-20261026`.
The Hipo checkout is `/Users/ontosai/github.com/ontosai/hipo-progressive-ingestion-20261026`.
Original checkouts were preserved. Planning is committed separately as `e9513bf1`.

## Implemented behavior

- Initial completion publishes an immutable readable baseline. The business Job
  stays done while independent structure, scope, vector repair and finalization
  tasks continue. No additional public ingestion selector or rollout framework
  was introduced.
- Queued dispatch gaps, expired claims, bounded retries and final upload failure
  recover through the existing worker and sweep. Publication checks the active
  revision and claim generation. Stale workers write distinct artifact keys;
  a fenced transaction selects the structure input mapping.
- Repair upserts the supplied page vectors and preserves successful vectors.
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
| Knowhere API scope publication/retry/webhook/QStash contracts | 21 passed |
| Knowhere worker Phase A/scope/reliability/retrieval contracts | 52 passed |
| Knowhere Ruff and Pyright (`make check`) | Passed |
| SDK complete suite | 400 passed across 22 suites |
| SDK lint, typecheck, build and changeset checks | Passed; three existing lint warnings |
| Hipo complete unit suite | 2,268 passed across 292 files; six existing skips |
| Hipo fresh PostgreSQL boundary suite | 32 passed |
| Hipo Chromium reader suite | Five passed across three files |
| Hipo typecheck and lint | Passed |

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

1. Verify healthy deployment configuration, provider access, migrations and
   halfvec support against the actual target environment. Earlier infrastructure
   observations are not current deployment evidence.
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

Immutable baseline/final archives, source PDFs and retained Hipo objects increase
storage and transfer. Processing uses separate mutable input storage and immutable
claim-specific outputs. SDK checksum verification uses temporary disk proportional
to compressed archive size, including the progressive Range path. These costs
must be included in capacity acceptance for both workers and the Hipo runtime;
green local tests do not prove the current server configuration is sufficient.
