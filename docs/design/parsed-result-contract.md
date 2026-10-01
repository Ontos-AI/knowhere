# Parsed result ZIP contract

**Status:** Draft — RFC only, no implementation in this change
**Related issue:** [#45](https://github.com/Ontos-AI/knowhere/issues/45)
**Prior proposal:** [nuemaan, 2026-06-12](https://github.com/Ontos-AI/knowhere/issues/45#issuecomment-4692240990)
**Observed against:** `main` @ `9489fa2c` (2026-09-14)

This document asks maintainers to lock three decisions before any models,
JSON Schema export, or worker validation land. It does not change runtime
behavior.

## Purpose

The parse-result ZIP (`chunks.json`, `doc_nav.json`, `manifest.json`) is the
de facto contract consumed by the Python SDK, Node SDK, notebooks, and
other apps. Today those three JSON documents are written from dict payloads
with no schema, no `schema_version` on `chunks.json`, and no pre-completion
validation. A worker change can ship a break to SDK consumers without failing
the job.

Issue #45 asked for detection before the job completes. nuemaan proposed
Pydantic models, a `validate_parse_result` helper, JSON Schema export, and
`make sync-contracts`, then asked three scope questions. Maintainer has not
replied. This RFC answers those questions against the current writers and
asks for an explicit go / no-go.

## Current writers (facts)

| Artifact | Writer | Informal version field today |
| --- | --- | --- |
| `chunks.json` | `ZipPackageWriter.write` via `{"chunks": formatted_chunks}` | none |
| `doc_nav.json` | `ZipDocNavigationBuilder.build_doc_nav` (or an already-enriched on-disk file) | `"version": "1.0"` |
| `manifest.json` | `ZipManifestBuilder.generate_manifest` | `"version": "2.0"` |

Call chain for a successful job:

1. `finalize_parse_success` (`apps/worker/app/services/document_ingestion/success_finalization.py`)
2. `_generate_result_package` → `ZipResultService.generate_zip_package`
3. `format_chunks` / `build_doc_nav` / `generate_manifest`
4. `ZipPackageWriter.write`
5. S3 upload, then `lifecycle_service.finalize_job_success`

`test_parse_task_contract.py` asserts the three filenames exist and checks a
few summary fields (`source_file_name`, `statistics.total_chunks`, chunk
`type`). It does not validate inner shape, required keys, or types.

`AGENTS.md` "Persisted Document Corpus Schema" describes the on-disk
`~/.knowhere/{corpus}` layout. That prose is useful but is not the SDK ZIP
contract: it omits `chunk_type=page`, and table `content` in the ZIP is often
a `tables/...` path rather than inline HTML (`test_table_asset_schema_contract.py`).

`doc_nav.json` is currently best-effort. `ZipResultService._build_navigation_outputs`
logs a warning and returns `None` on failure, and `ZipPackageWriter` then
omits the file. SDKs that assume the file is always present can already break
on a "successful" job.

## Decisions requested

### 1. Schema location

**Recommendation:** two-layer, Python models as source of truth.

- Author models in `packages/shared-python/shared/contracts/parse_result/`
  (Pydantic v2). Worker and shared tests import these directly. This matches
  nuemaan's proposal and keeps validation next to the ZIP writers in
  `packages/shared-python/shared/services/storage/`.
- Export JSON Schema to language-neutral `packages/contracts/parse_result/`
  so the Node SDK and any non-Python consumer can vendor files without
  depending on `knowhere-shared`.
- `make sync-contracts` regenerates the JSON Schema from the models. CI
  fails if generated files drift.

Do not put hand-written JSON Schema in `shared/` only: the Node SDK would
have to vendor Python package paths. Do not start with a separate
hand-authored `packages/contracts/` that Python then re-implements — that
duplicates the source of truth.

A later `packages/contracts` Python stub that only re-exports generated
schema is fine; it is not required for v1.

### 2. Contract violation: hard-fail the job

**Recommendation:** hard-fail. `status=failed`, no ZIP upload, structured
error naming the field path and `schema_version`.

This is the reading of #45 ("detect the break before it reaches consumers").
Soft-warn-and-upload still ships a malformed ZIP to SDKs.

Mount point:

- Validate **after** `formatted_chunks`, `doc_nav`, and `manifest` exist and
  **before** `ZipPackageWriter.write` / S3 upload, inside
  `ZipResultService.generate_zip_package`.
- Raise a domain exception (new `ParseResultContractException` or reuse
  `WorkerHandlingException`) that `finalize_parse_success` / the parse task
  already maps to `status=failed`.
- `user_message` names the artifact and field path
  (`chunks.json / chunks[3].metadata.page_nums`). `internal_message` may
  include the validator error. `internal_message` must not appear in
  `to_client()`.

Missing `doc_nav.json` (today optional) should be treated as a **v1
required-file** violation if maintainers agree SDKs depend on it. If
page-memory or fragment jobs legitimately omit it, say so here and keep it
optional in schema_version 1. Default proposal: required for successful
jobs.

### 3. Versioning policy

**Recommendation:** integer `schema_version` starting at `1`, independent of
the existing informal `version` strings.

- `schema_version` is a new integer field on all three JSON roots.
- Do **not** reinterpret `manifest.version` (`"2.0"`) or `doc_nav.version`
  (`"1.0"`) as the contract version. Those stay as observed fields in
  schema_version 1 so current consumers keep working.
- `chunks.json` has no version today. Adding `schema_version` is an additive
  root field. Consumers that ignore unknown keys remain compatible.
- Additive optional fields (new optional metadata key, extra stats counter):
  **do not bump** `schema_version`. Consumers must ignore unknown keys.
- Breaking changes (rename, remove, change type, make a previously optional
  field required): **bump** `schema_version`. Worker writes only the new
  version. Validator rejects payloads that do not match the version the
  worker claims to emit.
- No dual-write / mixed versions in one ZIP. One ZIP, one `schema_version`.
- Transition: validator accepts a missing `schema_version` as `1` for a
  single release, then requires the field. Call that out in the implementing
  PR.

Do not use semver strings (`1.1.0`) for this field. Integer comparison is
enough and matches nuemaan's "starting at 1".

## Observed schema_version 1 shape (lock this, do not invent)

This is the shape `ZipResultService` actually writes on current `main`,
including page-memory. Implementing PRs should encode this, not AGENTS.md.

### `chunks.json`

```json
{
  "schema_version": 1,
  "chunks": [
    {
      "chunk_id": "string",
      "type": "text | image | table | page",
      "content": "string",
      "path": "string",
      "metadata": {}
    }
  ]
}
```

Required per chunk: `chunk_id`, `type`, `content`, `path`, `metadata`.
`type` is a closed enum: `text`, `image`, `table`, `page`.

Shared metadata (always present in `format_chunks`):

- `length` (int)
- `summary` (string)
- `page_nums` (list of int)

Type-specific metadata the writer currently sets:

- `text`: `tokens`, `keywords`, `connect_to`
- `image`: `file_path` (when known), `keywords`, `tokens` (empty list)
- `table`: `file_path`, `keywords`, `tokens`, `connect_to`
- `page`: `keywords`, `connect_to`, optional `page_assets`

`connect_to` items are either a target string or an object with `target`,
`relation`, optional `ref`, `position: {start, end}`, `score`, `keywords`,
`same_as_owner` (`ConnectionPayload` in `chunk_connections.py`).

In-memory `ChunkPayload` also has `order`. The ZIP formatter **does not
write `order`**. Do not require it in schema_version 1.

Table `content` is often the asset path (`tables/table-1.html`), not the
HTML body. Image `content` is description text plus an asset ref. Do not
require HTML in `content` for `type=table`.

### `doc_nav.json`

```json
{
  "schema_version": 1,
  "version": "1.0",
  "file_name": "string",
  "stats": {
    "total_chunks": 0,
    "text_chunks": 0,
    "image_chunks": 0,
    "table_chunks": 0,
    "page_chunks": 0,
    "max_depth": 0
  },
  "sections": [
    {
      "title": "string",
      "path": "string",
      "level": 1,
      "summary": "string",
      "chunk_count": 0,
      "children": []
    }
  ],
  "resources": {
    "images": [{ "path": "string", "summary": "string" }],
    "tables": [{ "path": "string", "summary": "string" }]
  }
}
```

`stats.page_chunks` is already produced. A v1 schema that omits it would
be a regression.

### `manifest.json`

```json
{
  "schema_version": 1,
  "version": "2.0",
  "job_id": "string",
  "data_id": null,
  "source_file_name": "string",
  "processing_date": "ISO-8601 Z",
  "processing": {
    "page_count": null,
    "billing_status": null,
    "cost": { "micro_dollars": null, "credits": null },
    "timing": {
      "started_at": null,
      "completed_at": null,
      "duration_ms": null
    },
    "stages": {}
  },
  "statistics": {
    "total_chunks": 0,
    "text_chunks": 0,
    "image_chunks": 0,
    "table_chunks": 0,
    "page_chunks": 0,
    "total_pages": null
  },
  "HIERARCHY": {}
}
```

`strip_manifest_cost_fields` already removes `processing.cost_estimate`
before the ZIP write. The ZIP contract must not require that internal field.

## Testing plan (for the later implementation PR)

Do not replace `test_parse_task_contract.py`. Split coverage:

1. **Model unit tests** next to the new contracts package: happy path for
   each artifact; one violation per required field; unknown `type`;
   `internal_message` absent from `to_client()`.
2. **Writer unit tests**: `ZipResultService.generate_zip_package` calls the
   validator and does not write/upload when validation fails (mock writer).
3. **One parse-task contract assertion**: a deliberately malformed payload
   fails the job with `status=failed` and never uploads a ZIP. Keep the
   existing "three files present + counts" checks as the happy-path smoke.

## Non-goals (this RFC and any first implementation PR)

- Changing ZIP member names or moving files
- Adding a new ZIP format or SDK major version in the same PR as the
  validator
- Validating image/table binary bytes, only JSON shape
- On-disk `~/.knowhere` corpus files outside the job result ZIP
- New runtime dependencies beyond Pydantic (already in the tree) and the
  JSON Schema export toolchain if one is added
- Retrieval / BM25 / agent_explore

## Open questions for maintainers

1. Confirm schema location (shared-python models + generated
   `packages/contracts/`) vs a contracts-only top-level package.
2. Confirm hard-fail vs warn-and-upload.
3. Confirm integer `schema_version` starting at 1, additive fields without
   bump, breaking changes bump.
4. Is `doc_nav.json` required on every successful job, including fragment /
   image / page-memory?
5. Should `schema_version` be required on the first implementing release, or
   accepted-as-1 when missing for one release?

Please answer on #45. No code until those five are explicit.
