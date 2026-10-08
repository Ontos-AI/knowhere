# Canonical demo bundle reuse

## Scope

Demo materialization reuses the same immutable ZIP and raw artifacts across
users and namespaces. Jobs, job results, documents, publication transactions,
authorization and retrieval isolation remain owner scoped. Normal Worker parsing
continues to store its result artifacts by job identifier.

The previous demo path compressed and uploaded every artifact for each new job.
Two real local SpaceX materializations created 637 objects each and took 40.422 s
and 39.098 s from HTTP request to response. These are local end-to-end timings,
not production measurements or isolated publication transaction timings.
Each request duplicated 439,190,627 stored bytes, including a 215,559,924-byte ZIP.

After the change, the first canonical upload took 40.211 s. Two subsequent
materializations into separate namespaces took **5.483 s** and **5.337 s**,
respectively (about 86% lower than the two legacy samples). Both added zero
objects and rewrote zero objects; all 638 canonical objects retained their
ETags, sizes and modification times. The extra object is the completion marker.
This removes about 439 MB of duplicate object writes per repeat materialization.

Both repeat requests passed real HTTP checks of document image/table/page
assets, classic retrieval page/image assets, the public job ZIP URL, and archive
readback. Archiving one materialization left the other's shared assets readable.

## Storage contract

The content version hashes sorted relative file paths, lengths and file bytes,
including a bundle-format version. A bounded process-local memo avoids rereading
unchanged file bytes; changes to a file's size, mtime or ctime invalidate it.
Symbolic links and sources that change during hashing/upload are rejected.

Keys preserve the existing result storage layout:

```text
results/demo-canonical/<source-id>/<content-version>.zip
results/demo-canonical/<source-id>/<content-version>/<artifact-path>
results/demo-canonical/<source-id>/<content-version>/.bundle-ready.json
```

The marker is written only after ZIP and raw uploads complete. A failed partial
upload is never reused. A subsequent request can retry it. A renewable Redis
lease serializes first uploads across API processes; release compares the owner
token and an abandoned lease expires. Compression, hashing and storage calls run
off the API event loop. Cancellation waits for an active upload thread before
releasing its lease.

Reuse checks the completion marker and ZIP size without issuing one storage
request per raw artifact. If a raw object is deleted after completion, reuse
does not detect or repair it automatically. Recovery requires invalidating that
version's `.bundle-ready.json` and rerunning the preparation command against the
same source version. Protect the shared prefix from per-job lifecycle cleanup.

`JobResult.result_s3_key` records the canonical ZIP.
`JobResult.document_metadata.result_raw_prefix` records the canonical raw prefix.
Artifact readers use that metadata when present and retain the job-key fallback
for older results and ordinary Worker output. No database migration is required.

## DevOps enablement and rollback

1. Deploy the writer and compatible artifact readers together. The independent
   `DEMO_CANONICAL_BUNDLE_ENABLED` setting defaults to `false`.
2. Prepare bundles with the same deployed source files and result bucket, using
   the [preparation command](canonical-demo-preparation.md). This moves the
   initial ZIP compression and artifact upload ahead of user requests. A new
   unprepared content version still needs its first upload.
3. Enable `DEMO_CANONICAL_BUNDLE_ENABLED=true` on API instances after checking
   access to their configured result bucket and Redis. This does not change
   `KNOWHERE_PUBLICATION_STRATEGY` or retire any database index.
4. Materialize the same source into two namespaces. Verify identical ZIP and
   raw keys, and unchanged object timestamps/ETags on the second request. Fetch
   image/table/page artifacts and the ZIP through their public response URLs.
5. Roll back new writes by setting `DEMO_CANONICAL_BUNDLE_ENABLED=false` and
   restarting the API. Keep the compatible readers deployed: they continue to
   resolve existing canonical results while new jobs use individual bundles.
6. Do not delete canonical objects on per-document archive or expiration. A
   source/version can have references from multiple owners. Bucket lifecycle
   rules must preserve this prefix for the full lifetime of its references.

A binary rollback to a release without prefix-aware artifact readers requires
first restoring per-job raw objects for every canonical result, or retaining
the reader compatibility patch. Turning the switch off does not rewrite
existing result records.

The separate `DEMO_PUBLICATION_PREPARATION_CACHE_ENABLED` flag also defaults to
`false`. It requires canonical bundles and reuses only pure lexical preparation
inside the current API process. See [cache boundaries and measurements](demo-publication-preparation-cache.md).

[Legacy token lookup retirement](legacy-token-lookup-retirement.md) is an
independent database operation. Normal deployment preserves that index; enabling
either demo flag does not change it. Index restoration uses the operator command
at the same Alembic head and can take substantially longer than retirement.

## Verification boundary

Contract coverage targets reuse across namespaces, content changes, failed
uploads, concurrent creation and legacy artifact compatibility. Runtime
verification must also exercise actual HTTP, Redis/Celery Worker execution,
real parsing, publication, retrieval and archive. Local results do not establish
production latency or performance at a larger database size.

The real HTTP verifier caught a missing raw-prefix projection in classic
`map_unit_discovery` hydration: materialization and document asset endpoints
succeeded, while a retrieved page image returned 404. The query now projects
the artifact prefix alongside the job identifier. Artifact location is also
carried through connected/reference hydration, small-corpus loading and the
agent read/grep/table tools; their storage reads share the same prefix support.

The actual Worker Markdown regression used presigned local S3 upload and
confirm-upload, followed by Redis/Celery dispatch, the real parser, publication,
classic retrieval and archive. It completed in 3.240 s observed by the client;
the parser trace was 2.307 s. This small document verifies the ordinary Worker
path and is not a SpaceX parse benchmark.
After the canonical-reader changes, the Worker was restarted and the same real
queued path passed again in 3.278 s (job duration 2.906 s). All runtime test
documents were archived. Production configuration and indexes were untouched.

After the preparation-cache changes, a fresh API and Worker processed another
HTTP-created Markdown job in 4.300 s client-observed time (3.814 s job duration).
The fresh Worker log confirmed ownership; two chunks, classic retrieval and
archive passed. The configured summarization provider returned HTTP 401, so this
run exercised the parser's fallback. It does not validate successful external
LLM/PDF parsing or full SpaceX parsing latency.

Focused verification passed: six canonical/trace contracts, eight storage and
document lifecycle contracts, and 48 existing retrieval checks. The canonical
HTTP contract also exercises classic retrieval's shared asset URL after the
raw-SQL projection fix. Ruff, targeted Pyright and `git diff --check` passed.

Run the canonical regression contracts with:

```sh
uv run pytest -q apps/api/tests/contract/test_canonical_demo_bundle_contract.py \
  apps/api/tests/contract/test_demo_publication_trace_owner_contract.py
```
