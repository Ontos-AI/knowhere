# Shared demo backend validation — 2026-10-03

## Result and boundary

The backend was implemented and validated locally on branch
`feat/wangbinqi/shared-demo-corpus`. API and production Celery/gevent Worker ran
against isolated PostgreSQL 16, Redis, and LocalStack S3. Both service processes
used `demo_runtime`, a non-superuser role without BYPASSRLS. DeepSeek and Ali
provider calls were real; LLM mocking was disabled. The agent harness was the
OpenAI-compatible harness with DeepSeek. The real-provider validation run made
no production deployment, cloud mutation or Notebook change.

Timed runs used the local working tree, including pre-existing chunk-read
projection optimizations in `map_unit_index.py` and `serving_manifest.py`. Those
projection hunks and the earlier `prepare_demo_bundles.py` script edit are outside
this PR and remain local. Publication times below characterize that measured
tree, rather than a benchmark of the exact submitted tree. The submitted code
is separately checked in an isolated checkout. API and Worker contract suites
run sequentially because their fixtures share `/tmp/knowhere-api-tests`.
Three page-retrieval contracts now resolve live imports after runtime module
reset, so their provider/storage patches affect the functions they execute.

The shared upload/publication/read flow works. Follow-up fixes resolved the two
SpaceX failures found during initial validation: truncated hierarchy responses
silently published one Root chunk, and agent context compaction removed useful
read evidence, causing repeated reads until the token budget ran out.

The real SpaceX update now publishes 502 page chunks covering all 407 pages;
the largest section spans ten pages. Both readers completed classic and real
agent queries with citations to specific page ranges and the same new revision.
Historical reads and PDF byte ranges remain available, and reading creates no
jobs, chunks, token rows or stored assets. These checks cover the launch queries
described below, rather than a comprehensive retrieval quality benchmark.

## Automated verification

| Check | Result |
| --- | --- |
| Full API contract suite, including evidence-retention regression | 434 passed |
| API migration contracts after historical revision-scope corrections | 30 passed |
| Existing API unit tests in the full API run | 12 passed |
| Full Worker contract suite in the submitted checkout | 342 passed |
| Relevant Worker contracts: parsing, processing, bootstrap, publication trace | 29 passed |
| New shared demo contract file | 7 passed |
| Full page-memory contracts after hierarchy fix | 83 passed |
| Agent evidence-retention and shared demo contracts | 8 passed |
| `make check` | Ruff and Pyright passed |
| `git diff --check` | Passed |

The isolated full API command initially produced 473 passes and three migration
contract failures. One assumed the old revision was still head; two attempted
to downgrade through the new retained demo schema. The maintenance contract now
checks its captured initial head, and reversible token-index tests explicitly
target their historical migration chain. All 30 migration contracts then passed.
Only those test files changed after the full API run; runtime code was unchanged.
The isolated Worker command passed all 342 contracts after correcting the stale
test imports described above. Gitleaks found no leaks in the submitted tree.

Commands for the submitted checkout were the CI equivalents of
`uv run pytest apps/api/tests -q`, `uv run pytest apps/api/tests/migrations -q`,
`uv run pytest apps/worker/tests/contract -q`, Ruff and Pyright. The isolated
checkout reused the existing virtual environment with its own shared/API/Worker
paths; package dependencies were unchanged.

The local-development role bootstrap was also checked in an isolated
PostgreSQL 15 container using the actual init scripts. Repeating provisioning
succeeded; the runtime role had no superuser, BYPASSRLS, database-creation or
role-creation privileges. A table created afterward inherited DML grants, while
an INSERT without an RLS policy was rejected. The startup shell passed `bash -n`.

The new contracts exercise preparing/410 responses, maintainer authorization,
FORCE RLS, same shared data for two users, private isolation, concurrent source
claims, second-batch rollback, Worker target validation, update failures, retained
revisions, global cache generation, agent tools and final references, archive,
revision-aware media, traversal rejection, deletion protection, include/exclude,
section/type filters, no matches, unpublished/wrong-source revisions, retry after
failure, curated citation rebinding, and caller-specific hit statistics. The
Worker bootstrap contract verifies a bypassing role fails before consumers or
sidecars start. Tests use real PostgreSQL; provider fakes in contract tests are
separate from the real provider verification below.

This checkout has no general job cancellation/deletion API. Ordinary DELETE
remains unsupported; retained shared revision DELETE returns 409. No new cancel
endpoint or destructive job cleanup was added.

## Real HTTP and Worker verification

Every parsed input below used HTTP create job, PUT original, confirm upload,
GET polling, and HTTP reads. Worker was started through `worker.py` with the
production gevent pool and psycogreen. An earlier SpaceX job correctly failed the
old local 100 MiB limit; validation then configured the planned 300 MiB limit on
both processes. The initial successful parse exposed the hierarchy bug. A real
HTTP update after fixing it preserved the document ID and retained that earlier
revision.

| Input | Track | Published chunks | Parser time | Full Worker task |
| --- | --- | --- | --- | --- |
| Synthetic DOCX with text, image and table | chunk | 5: 3 text, 1 image, 1 table | 6.643 s in the final timed update | 8.106 s |
| Tesla Q4 2025 authoritative original | page_memory | 73: 37 page, 36 image | 348.778 s | 357.56 s |
| SpaceX initial import before hierarchy fix | page_memory | 1 page chunk after hierarchy fallback | 1,217.407 s | 1,243.392 s |
| SpaceX authoritative original update, 153,808,384 bytes / 407 pages | page_memory | 502 page chunks | 2,546.914 s | 2,585.419 s |
| Same synthetic DOCX uploaded privately | chunk | 5 | 6.851 s | HTTP completion observed after 10.107 s |

The final demo DOCX request measured create 29 ms, original PUT 25 ms,
confirm 55 ms, retained result ZIP/assets upload 46 ms, and database publication
including commit 267.242 ms. Polling observed READY after 9.917 s. Queue delivery
was within the same one-second Worker log timestamp bucket; that is a bound,
not a precise queue latency measurement. Private DOCX measured create 60 ms,
PUT 21 ms, confirm 128 ms, and publication 303.254 ms.

The large PDF jobs ran before the new upload timer and publication tracing were
enabled. Their original-upload figures (Tesla 0.246 s, SpaceX 1.351 s) include
the then-combined helper interval, rather than separately measured PUT/confirm.
ZIP/assets upload and database publication are only coarse log bounds for these
initial PDF jobs. The corrected SpaceX update has independent timings below.
The helper now records create, PUT and confirm separately, and the Worker records
`worker.result.assets_upload` independently of the publication trace.

HTTP validation checked catalog no-store, document lists in both API versions,
chunk reads, originals, DOCX image/table assets, Tesla page images, and normalized
PDF page-citation-source. Storage responded 206 to byte ranges, with correct
PDF, DOCX, PNG and HTML MIME types. Invalid manifest paths returned 404; ordinary
demo upload returned 403; materialization returned 410. Tesla, DOCX and corrected
SpaceX agent episodes completed with revision-bearing sources and references.

Two users executed classic/shared reads and agent retrieval. During this read
interval, before/after counts were identical:

| Resource | Before | After |
| --- | ---: | ---: |
| Jobs | 5 | 5 |
| Demo chunks, including retained revisions | 83 | 83 |
| Demo token rows | 13,530 | 13,530 |
| Private chunks/token rows | 0 / 0 | 0 / 0 |
| S3 objects | 490 | 490 |

The private upload, publication diagnostic, and later manual DOCX updates took
place after that interval and intentionally changed counts. A real DOCX update
retained `ddoc_706bbc5ce1f9`; both readers subsequently observed revision
`5d0eb1ca-5d35-42bb-b04b-a66488dcf26d` while the old
`a56acbeb-0ad3-4eee-adad-8eacefcfc7cb` remained readable through pinned chunk and
original requests. The recovery helper resumed completed Tesla/SpaceX jobs
without creating new parsing jobs and recorded their own immutable revisions.

## SpaceX follow-up acceptance

The updated source retains `ddoc_ceb0d9f0c884` and publishes revision
`c7e46329-9185-4d4d-a459-f2cf6e303735` through job `job_a0a5f79cfb1f`.
The old `73863515-a0d4-420b-adb0-d8efbaca6eaa` revision remains readable.
Fine hierarchy classified 842 observed titles into 562 skeleton nodes; one real
malformed response batch was detected and recovered by smaller retries.
Publication committed 553 sections, 502 chunks, 477 map units and 81,833 tokens.

| Phase | Measured duration |
| --- | ---: |
| Profiling | 822.010 s |
| Title rendering / detection | 65.890 s / 546.147 s |
| Fine hierarchy | 26.217 s |
| Page rendering / tagging | 65.658 s / 420.940 s |
| Node assembly, including summaries | 599.273 s |
| Complete parser | 2,546.914 s |
| Result/assets upload | 7.169 s |
| Database publication, including commit | 18.081 s |
| Complete Worker task | 2,585.419 s (43 min 5 s) |

The HTTP helper separately measured job creation at 0.102 s, original PUT at
0.992 s and upload confirmation at 0.128 s. Polling observed completion after
2,588.824 s. Queue delivery was not independently measured with subsecond
precision.

All 407 pages occur in the published page metadata. Maximum raw chunk content
is 42,365 characters, compared with 1,467,478 in the initial Root chunk. This
revision has page images and its normalized source PDF, but no standalone image
or table chunks; DOCX and Tesla separately exercised those asset paths.

Classic query `Falcon 9 launch capabilities payload capacity` returned three
results with page ranges 31–32, 167–168 and 107–108. Reader request times were
2.458 s and 1.930 s. Real agent query `Compare the launch capabilities and payload
capacities of Falcon 9 and Falcon Heavy.` finished in 9.439 s and 9.831 s,
returning three and four references. Both sets included specific Falcon launch
sections and new-revision pins. Citation pages were checked against text extracted
from the backed-up original PDF. The citation PDF served 206 with PDF MIME for
byte range 0–63.

The original query that exhausted its token budget, `What are the launch
capabilities of Falcon 9 and Falcon Heavy?`, was also repeated after bypassing
each reader's local query cache. Both episodes finished in 7.017 s and 8.075 s
with three references to page ranges 31–32, 167–168 and 208–209. API restart alone
was not used as proof of cache invalidation. The traces match the original
recall/grep and first read, then perform one complementary read and finish.
They consume 43,185 and 43,242 tokens, compared with 111,394 tokens in the
original budget-exhausted episode.

During those two-reader checks, jobs remained 11, demo chunks including history
remained 668, token rows remained 99,883, and S3 objects remained 912. No parser
job or materialization was created. Agent context retention was changed only
after this new revision existed, so agent verification did not reparse the file.

The evidence-retention contract reproduces a multi-section read whose second
fact is truncated, followed by a complementary read. Before the fix, evidence
vanished after two turns and reads alternated until the episode budget expired.
After the fix, the episode finishes with both references while stale discovery
content is compacted. Successful evidence messages with valid refs stay visible;
per-result text caps and episode budgets remain enforced.

See [the hierarchy fix](spacex-hierarchy-fix.md) for batching, strict response
validation, cross-batch nesting and preservation of opening body pages.

## Publication diagnostic

The same captured Tesla ZIP's 73 chunks were replayed once into a private scope
and once into a demo scope using gevent/psycogreen and the real runtime role.
Both committed 45 sections, 73 chunks, 36 map units and 4,345 token rows.

| Storage | Publication including commit | SQL statements | SQL time |
| --- | ---: | ---: | ---: |
| Private execute_values compatibility path | 1,366.279 ms | 51 | 461.289 ms |
| Demo INSERT under FORCE RLS | 1,087.970 ms | 58 | 540.103 ms |

These are single diagnostic samples, with different existing namespace snapshot
and graph sizes and private cold-start effects. They show the storage adapter
can publish the same payload; they do not prove demo is faster. Replay is not
parsing E2E validation. The diagnostic demo was archived through the normal API;
its history is retained in the isolated validation database.

## Backup and delivery artifacts

All 27 authoritative originals were format-validated and copied into the local
persistent `.demo-originals/20261003` directory; every size and SHA-256 was checked
again after copying. Total: 225,749,274 bytes. The inventory and backup directory
are Git-ignored. A prior run independently verified the Mac backup as well;
this run did not reverify Mac availability after SSH became unreachable.

Sanitized JSON evidence is preserved under
`.demo-originals/validation-20261003/`. Temporary service credentials and raw
provider logs are excluded. Local API uses port 55005; isolated PostgreSQL,
Redis and S3 use 55543, 56379 and 54566. Services may be restarted from the local
validation environment, but credentials must never be copied into delivery
documents.

See [the rollout guide](shared-demo-rollout.md) for migration credentials,
maintainer/runtime configuration, backup and resumable recovery commands,
client breaking changes, query examples, coordinated cutover, and rollback.
Notebook must still migrate before accepting the complete end-user demo flow.
