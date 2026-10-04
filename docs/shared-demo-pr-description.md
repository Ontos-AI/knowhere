## Summary

Demo selection previously created a private copy for each user. We publish each source once into twelve dedicated PostgreSQL tables in `__knowhere_demo__`, with stable document IDs, retained revisions/assets, shared retrieval and caller-specific statistics. Optimized batched publication is now the only implementation.

Maintainers use the existing V1/V2 upload, confirmation and Worker parser flow. API and Worker validate the server-owned corpus target; FORCE RLS rejects unapproved writes and both services reject database roles that bypass RLS. Publication atomically switches the revision, manifests and global generation. Document, chunk, media, agent tools and citation reads honor captured revision pins.

Real SpaceX validation exposed two failures addressed here: hierarchy JSON truncation silently produced a single Root chunk, and agent context compaction removed useful evidence and caused repeated reads. Bounded hierarchy batches validate every heading, retry malformed output and fail the import if recovery is exhausted. The OpenAI harness retains successful evidence while compacting old discovery results.

### Client migration

- `POST /api/v1/demo/materializations` returns **410 `DEMO_MATERIALIZATION_REMOVED`**. Existing private copies remain accessible through ordinary APIs.
- Save catalog `canonical_document_id` and `job_result_id`; use the shared namespace directly. Hide a demo source locally instead of archiving the shared document. Mixed private/demo retrieval needs separate requests.
- Pass `job_result_id` when opening historical citations, chunks, originals and assets. Old static chunk IDs are not stable after reparsing.
- Catalog is `no-store`. Explicit preparing sources return 409; an empty demo corpus returns 503. The initial migration seeds directory metadata without READY content.

```json
{
  "namespace": "__knowhere_demo__",
  "query": "What are SpaceX's launch capabilities?",
  "include_document_ids": ["<catalog canonical_document_id>"],
  "use_agentic": false
}
```

## Verification

- 434 API contracts, 30 migration contracts and 342 Worker contracts passed. The full API run found three stale migration-contract assumptions; their revision scope was corrected and the complete migration suite rerun successfully. Runtime code was unchanged after that full run. Existing API unit tests also passed; no new unit tests were added.
- Ruff, Pyright, staged diff checks and a redacted Gitleaks tree scan passed in an isolated checkout of the submitted code.
- Real local HTTP create → PUT → confirm → poll → retrieval/media checks used the production Celery/gevent Worker, enforced RLS, Redis, LocalStack and real providers for mixed DOCX, Tesla and the 153,808,384-byte SpaceX original.
- SpaceX now has 502 page chunks covering all 407 pages. The originally failing agent query finishes for both readers in 7–8 seconds with citations to pages 31–32, 167–168 and 208–209. Historical reads and PDF 206 Range responses passed; readers create no jobs, chunks, tokens or stored assets.
- All 27 originals were backed up and SHA-256/format validated. Full SpaceX processing took about 43 minutes. Measured publication took 18.081 seconds; timed runs included two pre-existing local projection optimizations excluded from this PR, so this is not a benchmark of the exact submitted tree.
- Notebook migration and production deployment are separate; the complete end-user flow awaits that client upgrade.

## Deployment Notes

- Remove `KNOWHERE_PUBLICATION_STRATEGY`. Set `DEMO_MAINTAINER_USER_IDS` and use a NOSUPERUSER/NOBYPASSRLS runtime `DATABASE_URL`; the default maintainer set is empty.
- Run additive migration `4d5e6f7a8b9c` with separate management credentials (`MIGRATION_DATABASE_URL`). It stops on private documents already using the reserved namespace. Grant runtime DML permissions after migration.
- Verify the original-file backup before cutover. Stop old materialization admission and drain tasks before updating API/Worker together. Restore Tesla first, then SpaceX, then other sources using the resumable helper; service startup does not trigger parsing.
- Roll back API/Worker images together after stopping imports and draining/cancelling new tasks. Retain additive tables, originals and revision assets; do not run a destructive down migration.

See `docs/shared-demo-rollout.md` for backup/runtime grants, upload examples, recovery commands, client migration and rollback, and `docs/shared-demo-validation-20261003.md` for measured results and limitations.

## Checklist

- [x] Contract tests added or updated for changed behavior
- [x] Public documentation, examples and migration contracts updated
- [x] Additive migrations and runtime-role provisioning verified locally
- [x] Logs, errors and validation paths avoid leaking credentials
- [x] Breaking changes and release ordering documented
