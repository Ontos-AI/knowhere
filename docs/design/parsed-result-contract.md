# Parsed result ZIP contract

**Status:** Draft implementation; policy choices below remain unaccepted upstream.
**Related issue:** [#45](https://github.com/Ontos-AI/knowhere/issues/45)

The worker emits one versioned result ZIP. Its three JSON roots contain additive
`schema_version: 1`: `chunks.json`, `doc_nav.json`, and `manifest.json`. The existing
`doc_nav.version` (`"1.0"`) and `manifest.version` (`"2.0"`) remain unchanged.
Pydantic v2 models in `packages/shared-python/shared/contracts/parse_result/` are
its source of truth. Generated Draft 2020-12 JSON Schema lives in the
language-neutral `packages/contracts/parse_result/` directory for SDK vendoring.
Schema `$id` values identify the documents; they do not imply a hosted registry.

## Producer boundary

`ZipResultService.generate_zip_package` validates the prepared JSON before
calling the writer. `ZipPackageWriter` validates the closed ZIP before returning
its checksum. This precedes `finalize_parse_success` uploading results and
calling `finalize_job_success`. A contract violation raises the permanent domain
exception `ParseResultContractException`; it follows the existing task failure
handling and is not a transient storage retry. Invalid ZIPs are removed and are
never returned to the upload path.

New successful packages require all three roots. Navigation generation failure,
which previously logged a warning and omitted `doc_nav.json`, now fails the
package. The already enriched navigation is retained, including extension fields
such as `top_summary`; no on-disk corpus files are rewritten.
Missing, unreadable, or unrecognized navigation keeps the existing rebuild
fallback. A readable object claiming an unsupported `schema_version` fails before
that fallback, even when its section layout is not recognized.

Multiple chunks may reference the same image, table, or page asset. The writer
stores one ZIP member per asset path. Repeated paths with different contents
fail with `conflicting_asset_path` rather than creating ambiguous ZIP entries.

## Version and compatibility policy

- Version 1 captures current text, image, table, and page chunk shapes. Table
  `content` may contain an asset path rather than inline HTML. Text `tokens` may
  contain an integer count or a string list. `order` is not required.
- Required scalar fields are strictly typed. Shared metadata requires `length`,
  `summary`, and `page_nums`. Type-specific metadata, including page citation
  assets and connections, is validated when present. Nested sections may omit
  `level` in existing enriched navigation; their nesting defines depth.
- Unknown fields are accepted and retained, including optional processing and
  parser metadata. Additive optional fields do not bump the integer version.
  Removing, renaming, or changing required fields/types requires a new version.
- One ZIP has one supported version. Unknown or noninteger versions fail. The
  legacy string `version` fields remain independent of `schema_version`.
- `validate_parse_result` and `validate_parse_result_archive` default to a
  compatibility read: missing root versions are interpreted as legacy v1 and
  returned as warnings. Legacy unversioned manifests may omit navigation; a
  package declaring version 1 must include it. Producer calls explicitly set
  `allow_legacy=False`. The compatibility reader has no silent expiry; removing
  legacy support requires a separately reviewed change.

```python
from shared.contracts.parse_result import validate_parse_result_archive

result = validate_parse_result_archive("result.zip")
print(result.manifest.schema_version)
print(result.warnings)  # legacy-version/navigation compatibility notices
```

The helpers make no network calls. Consumers can vendor the JSON Schema and use
any Draft 2020-12 validator for individual JSON shapes. Cross-artifact checks and
ZIP membership checks require equivalent consumer logic or the Python helper.
This PR does not change the standalone Node or Python SDK repositories.

## Hard failures and warnings

Hard failures include malformed required JSON fields, unsupported versions,
missing required artifacts, disagreement between actual chunk types/counts and
manifest/navigation statistics, and invalid connection character spans. Manifest
heading hierarchy is recursive string-keyed dictionaries with dictionary leaves.

Archive validation reads members without extraction. It rejects unsafe member
names, duplicate members, malformed JSON (including duplicate keys and nonfinite
numbers), missing referenced `metadata.file_path` / page `artifact_ref` assets,
and unreadable archives. Asset references must be relative paths under `images/`,
`tables/`, or `page_citation_assets/`. Binary bytes, image dimensions, and model
output quality are outside this contract. Consumer reads default to a 128 MiB
limit for each JSON member; callers can choose `max_json_bytes`. The local writer
validates its generated archive without that size limit.

Only legacy root-version omission and legacy navigation omission are warnings.
Errors expose the existing canonical `INTERNAL_ERROR` and a stable details object:

```json
{
  "reason": "PARSE_RESULT_CONTRACT_VIOLATION",
  "schema_version": 1,
  "violations": [{
    "artifact": "chunks.json",
    "field": "chunks.0.metadata.length",
    "reason": "int_type"
  }]
}
```

Error details contain artifact and field locations and machine-readable reasons,
never document values, Pydantic input/context, archive paths, or hierarchy titles.
The canonical exception logger records these structured fields without the
validation traceback, so diagnostic log sinks cannot expand document locals.

## Updating schemas

```bash
make sync-contracts
make check-contracts
```

The check runs in PR CI and fails when checked-in schema differs from generated
models. Fixtures cover all four chunk types, enriched navigation, table asset
paths, and page citation assets. Unit tests validate fixtures with both Pydantic
and JSON Schema and exercise actual ZIP writing without external accounts.
Worker task contract tests cover successful versioned packages and a malformed
producer payload failing before upload.

## Draft choices for review

The schema location, hard failure policy, required navigation for newly emitted
packages, integer versioning, and continued legacy compatibility are local design
choices implementing the unanswered scope questions in #45. They have not been
confirmed by maintainers. The implementation is additive for successful current
JSON output, with the intentional change that malformed output fails the job
instead of reaching SDK consumers. There are no database migrations, new runtime
dependencies, environment variables, or ZIP member renames.
