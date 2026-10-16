# Shared demo corpus rollout and client migration

This backend replaces personal demo materialization with one shared corpus in
`__knowhere_demo__`. Publication always uses the optimized implementation.
Remove `KNOWHERE_PUBLICATION_STRATEGY` from deployment settings; no switch enables
the new demo path. Historical benchmark baseline/candidate labels are retained.
This document defines the rollout contract for the shared demo backend.

## Deployment and recovery snapshot — 2026-10-05

The shared demo backend was deployed in staging and production. The production
recovery completed on 2026-10-05 with all 27 catalog sources READY, 2,562 demo
chunks, generation 27, and 25.26 credits charged for parsing. The runtime
database role is neither a superuser nor a `BYPASSRLS` role, and all twelve demo
tables use FORCE RLS. HTTP checks covered catalog, classic retrieval, agent
retrieval for Tesla/SpaceX/Microsoft, document and chunk reads, originals,
assets, and page-citation sources. Evidence is retained in the ignored local
directory `.demo-originals/production-restoration-20261005/`; it is not a source
artifact and must not be committed.

The restored sources use `/api/v2/jobs`: PDF and PPTX inputs follow the
`page_memory` track, while DOCX inputs follow the `chunk` track. `/api/v1/demo/catalog`
is the read-only catalog endpoint. The old materialization endpoint returns 410.
Notebook/client migration remains a separate follow-up before the complete
end-user Add Demo workflow is considered finished.

## Storage and access

The existing PostgreSQL database gains twelve dedicated `demo_*` tables.
Private documents remain in their existing tables. A source's `data_id` is its
unique `demo_source_id` (up to 128 characters); `ddoc_...` document IDs remain
stable across reparses. Source directory metadata is seeded, but no old static
chunks are imported as READY content. Publication atomically writes sections,
chunks, map units, tokens, completeness markers, manifests, graph, generation
and the current revision. Demo INSERT batches contain at most 1,000 rows.
PostgreSQL prohibits COPY FROM into an RLS table, so private COPY and Worker
psycogreen/execute_values paths remain separate from demo INSERTs.

Only user IDs in `DEMO_MAINTAINER_USER_IDS` may create, replace or archive demos;
the default is empty. They must also hold the normal API write permission.
The Worker revalidates durable job target/source/document metadata and identity.
Moves between private and demo scopes are rejected. Other authenticated users
can query the same shared documents. Caller identity remains the basis for
authentication, billing, limits, cache keys and hit statistics.

All dedicated tables use FORCE RLS. Request processes must use a non-superuser
role without BYPASSRLS. API and Worker refuse to start with a bypassing role.
The server sets transaction-local maintenance/caller context after authorization.
Database credentials are internal service credentials, never end-user access.
Public content policies require an active source and a complete bound revision;
directory reads can include preparing metadata. RLS UPDATE of invisible rows
affects zero rows; unauthorized INSERT fails. Hit-stat policies bind the actual
caller. Retained revisions restrict deletion of their jobs/results even after
archive. API job deletion requests for these revisions return 409. This checkout
has no general job deletion/cancellation API; ordinary jobs remain retained
processing records, and DELETE remains unsupported for them.

## Backup gate

Use the authoritative originals in `knowhere-storage-staging`, not public URLs.
`scripts/demo-original-sources.json` freezes all 27 source mappings from the
existing catalog and each `app/data/demo_documents/<directory>/manifest.json`.
Each mapping uses `uploads/<manifest.job_id><original extension>`.

```bash
uv run --all-packages python scripts/backup-demo-originals.py \
  --profile knowhere --bucket knowhere-storage-staging \
  --directory "$HOME/knowhere-demo-originals/20261003"
```

The inventory records source ID, original job ID, S3 key, filename, actual size,
SHA-256 and format validation. The command checks every PDF page, DOCX archive
CRC and document structure, and fails the gate if any object is missing or
invalid. Resume reruns reuse only objects whose size and source ETag still match.
Do not switch with an incomplete inventory. These S3 originals have no versioned
recovery copy, so preserve an independent persistent backup.

The original backup inventory contains 27 verified originals totaling
225,749,274 bytes. Use a persistent operator backup directory; a temporary
validation copy is not a recovery backup. The production restoration used the
verified inventory and retained its validation report under
`.demo-originals/production-restoration-20261005/`.
That evidence directory is Git-ignored; preserve the original-file backup and
evidence independently before deleting the checkout. The helpers can run on any
host with the required credentials and persistent storage.
SpaceX is 153,808,384 bytes / 407 pages. Ensure **both** API and Worker
configure `MAX_FILE_SIZE=314572800`; older local configurations may still have
a 100 MiB limit.

## Migration and runtime roles

Stop if this preflight returns rows:

```sql
SELECT document_id, user_id FROM documents
WHERE namespace = '__knowhere_demo__';
SELECT current_user, rolsuper, rolbypassrls FROM pg_roles
WHERE rolname = current_user AND (rolsuper OR rolbypassrls);
```

Migration `4d5e6f7a8b9c` repeats the reserved-namespace conflict check. It is
additive and freezes its DDL/catalog independently of future ORM changes.
Run Alembic using a separate schema-management connection. Supply
`MIGRATION_DATABASE_URL` only to the migration process; use the nonprivileged
`DATABASE_URL` for API and Worker. Provision existing auth tables/users with
management credentials before starting runtime processes.

An administrator can provision a runtime role and grant existing service DML:

```sql
CREATE ROLE knowhere_runtime LOGIN NOSUPERUSER NOBYPASSRLS;
-- Set its password through your secret manager, not a checked-in SQL file.
GRANT CONNECT ON DATABASE "Knowhere" TO knowhere_runtime;
GRANT USAGE ON SCHEMA public TO knowhere_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO knowhere_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO knowhere_runtime;
-- Execute for the actual migration table owner:
ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO knowhere_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT USAGE, SELECT ON SEQUENCES TO knowhere_runtime;
```

The runtime role does not need schema ownership, superuser or BYPASSRLS. Preserve
existing deployment-specific function permissions and extensions. Migration
credentials must retain DDL permissions; runtime credentials must not replace
them. Verify both processes' effective roles before accepting traffic.

For the repository's local Docker stack, `deploy/local-dev/start-dev.sh`
provisions `knowhere_runtime` on both new and existing volumes. The API/Worker
examples use that role; the local password is development-only. Run migrations
separately before starting either service:

```bash
cd apps/api
MIGRATION_DATABASE_URL=postgresql+asyncpg://root:root123@localhost:5432/Knowhere \
  uv run alembic upgrade heads
```

Do not put the root connection in the API/Worker `DATABASE_URL`. Local role
provisioning preserves an existing runtime password; adapt copied examples if
that password has already been changed.

## Coordinated cutover

1. Complete backup, contracts and real HTTP/Worker validation.
2. Check for reserved private-namespace conflicts and role bypass.
3. Run additive migrations; confirm the directory is planned with no READY data.
4. Configure maintainer IDs and runtime credentials; remove strategy settings.
   Before deploying ECS, add `DEMO_MAINTAINER_USER_IDS` to the environment's
   runtime secret. Both task definitions require that key. Use a comma-separated
   list of authorized user IDs, or an empty string to disable demo writes until
   a maintainer is selected. Adding an API key is not required.
5. Stop old materialization admission and drain its tasks before updating API and
   Worker together. Old tasks must not execute new publication code.
6. Verify private upload/retrieval, catalog preparing state and materialization
   410 before manually restoring sources. Demo downtime is expected during the
   initial cutover; this step is complete for the current deployment.
7. Restore Tesla first as a smoke test, then SpaceX, then remaining inventory.

No service startup automatically parses demos. A failed source does not block
other READY sources. A failed update leaves its earlier complete revision active.
Initial failures remain unavailable. Keep historical revisions and all assets;
there is no automatic garbage collection.

## Manual HTTP restoration

Provide a maintainer API key through `KNOWHERE_DEMO_API_KEY` in your environment.
Do not put it in command arguments or commit it. The helper uses v2 jobs,
uploads the verified file through the returned PUT capability, confirms upload,
polls with increasing intervals, and checks the READY catalog.

```bash
uv run --all-packages python scripts/restore-demo-originals.py \
  --api-url https://your-knowhere-host \
  --inventory "$HOME/knowhere-demo-originals/20261003/inventory.json" \
  --source demo-tsla-q4-2025 --state-directory ./demo-restoration

# Repeat with --source demo-spacex-s1, then omit --source for all inventory.
# Default concurrency is 2. Recorded jobs are always resumed.
# --retry-failed creates a new job after failure; --reparse-ready creates
# a new revision for already completed recorded jobs.
```

State files persist each job/document/revision and a sanitized result report.
They omit provider secrets and signed URLs. Waiting-file jobs resume through
`POST /api/v2/jobs/{job_id}/upload-url`, which renews the upload capability only
for the owner with write permission and a valid corpus target. Interrupting the
helper stops polling; it does not cancel Celery processing. Resume follows the
same recorded job. To stop an import before rollback, drain the Worker or use
the existing operator failure/stale-job lifecycle; do not forcibly kill gevent
greenlets. Retry failed imports with a new job, retaining the old ledger entry.
GET jobs now returns the immutable `job_result_id` when available. The helper
validates the recorded source and original hash, and verifies this exact revision
through document reads; it does not attribute a later concurrent publication to
the earlier job.

The equivalent creation request is:

```http
POST /api/v2/jobs
Authorization: Bearer <maintainer-api-key>
Content-Type: application/json

{"namespace":"__knowhere_demo__","source_type":"file","file_name":"spacex-s1.pdf","data_id":"demo-spacex-s1"}
```

PUT the original using `upload_url` and `upload_headers`, POST `{}` to
`/api/v2/jobs/{job_id}/confirm-upload`, then GET the job until done/failed.
The source claim serializes updates across API processes; another active import
returns 409. After completion inspect catalog, retrieval, chunks, original,
image/table/page assets and v2 `files/page-citation-source`.

## Client breaking changes

`POST /api/v1/demo/materializations` now returns 410
`DEMO_MATERIALIZATION_REMOVED` with shared-namespace migration guidance. Existing
personal copies remain private documents accessible through ordinary APIs.
Old static canonical/chunk IDs are not IDs for newly parsed shared content.

Catalog uses `Cache-Control: no-store`. `sources` contains READY sources only;
`import_status` and official-library planned entries describe processing/failure.
Catalog returns stable `canonical_document_id`, source ID, `job_result_id`,
namespace and actual parsed counts/size. Unmatched curated example citations
are omitted until uniquely matched or manually corrected.

Query one namespace at a time:

```json
{
  "namespace": "__knowhere_demo__",
  "query": "What are SpaceX's launch capabilities?",
  "include_document_ids": ["<catalog canonical_document_id>"],
  "use_agentic": false
}
```

Mixing private sources and demos requires separate namespace requests and
client-side result merging. Retrieval source/evidence/referenced chunks include
their `job_result_id`. Document/chunk/demo original/assets and v2 page-citation
source reads accept an optional `job_result_id`: omitted means current complete
revision; specified means a complete revision belonging to that source. Demo
chunk `id` is the dedicated row ID; `chunk_id` is the content ID. Preserve both.
Original/assets issue short-lived signed 307 redirects with MIME/disposition;
object storage serves Range. Only exact paths in that revision's asset manifest
are accepted, with traversal rejection.
Chunk and retrieval asset projections use the same manifest validation and
one-hour MIME-aware signatures. Demo page previews reuse published page images
or the retained normalized PDF; reader requests never create cropped PDFs.

| State | Response |
| --- | --- |
| No READY demos in catalog | 200, `sources=[]`, official library planned |
| Query whole corpus without READY sources | 503 `DEMO_NOT_READY` |
| Explicitly request a known preparing source | 409 `DEMO_NOT_READY` |
| Unknown, archived, or wrong-source revision | 404 |
| Some READY sources | Query READY subset; other sources remain preparing |

Notebook needs a separate change before the complete user workflow can be
accepted: remove materialization; save shared source/document/revision; split
private/demo requests with include IDs; hide local demo selections instead of
archiving shared documents; pass revision into citations and media reads; key
chunk caches by revision; make catalog no-store; update folder/exclusion maps;
remove personal/static citation remapping; render 410/409/503 migration/preparing
states. Other clients using static IDs or materialization require the same migration.

## Verification and rollback

Contract tests cover private publication's COPY/gevent compatibility and shared
HTTP/RLS publication/reads, source claims, history, cache generations and archive.
Real validation runs API, production gevent Celery Worker, PostgreSQL, Redis and
S3-compatible object storage through HTTP job creation/upload/confirm/poll.
Provider calls must be real; payload replay is only a publication performance
diagnostic. Record API/upload, queue, parse, asset upload and publication timings
separately. Compare the same captured payload once between private and demo
storage. Multiple readers must not create jobs/chunks/tokens or reupload assets.
Full SpaceX parsing time is separate from user selection/read time; no ten-second
full-parsing promise is made.

Before downgrade, stop demo import admission and drain or fail active new jobs.
Restore previous API and Worker images together. Preserve all additive tables,
originals and revision assets; do not run a destructive down migration. Demo may
remain unavailable during rollback. Re-enable imports only after the compatible
pair is running. Production deployment and Notebook changes are separate from
this backend implementation.

See [the local verification report](shared-demo-validation-20261003.md) for
the historical local measurements and SpaceX hierarchy/agent fixes. That local
run covered all 407 SpaceX pages; the production recovery snapshot indexed
388/407 pages. Production READY means a complete publication of the parser's
output, not full physical-page coverage. The page-memory hierarchy/TOC scope
limitation remains a separate parser follow-up. The production snapshot above
comes from the retained restoration evidence, not the local validation report.
