# Batch Validation, Offline Verification, and Production Local Provider Design

## Objective

Advance the standalone local MinerU MVP through three independently testable stages:

1. validate a corpus of existing repository test/demo documents and generate machine-readable and human-readable reports;
2. provide an external Windows Firewall control that can prove the model process completed while outbound traffic was blocked;
3. make the normal production PDF ingestion path select the local MinerU process behind an opt-in feature flag while preserving the current cloud default.

The stages are ordered. Batch validation must work without production integration. Offline verification wraps that batch command without changing parser behavior. Production routing reuses the already-tested local process boundary.

## Existing document corpus

The committed default corpus uses only files already under `tests/`, `demo/`, or `fixtures/` in the two repositories:

- MinerU `tests/unittest/pdfs/test.pdf`;
- MinerU `demo/pdfs/small_ocr.pdf`;
- MinerU `demo/pdfs/demo1.pdf`, `demo2.pdf`, and `demo3.pdf`;
- MinerU `demo/office_docs/docx_01.docx`;
- Knowhere `apps/worker/tests/fixtures/sample_3pages.pdf`;
- Knowhere `apps/worker/tests/fixtures/sample_1000words.docx`;
- Knowhere `apps/worker/tests/fixtures/sample_chinese_600chars.docx`.

The validator will resolve corpus entries through named roots (`knowhere` and `mineru`). It will reject absolute paths, `..`, symlink escapes, unsupported suffixes, duplicate IDs, and roots outside the two explicitly supplied repository directories. The report will contain source IDs, filenames, tags, hashes, sizes, timing, package counts, fidelity classifications, findings categories, and errors, but not extracted document content.

## Stage 1: Batch validator

### Components

- `validation_corpus.py` owns the JSON corpus contract and safe path resolution.
- `validation_runner.py` executes `build_codex_review_package()` sequentially, samples process-tree RSS with `psutil`, audits the resulting package, and produces normalized result records.
- `validation_report.py` writes atomic JSON and escaped standalone HTML reports.
- `validate_codex_export_corpus.py` is the CLI entrypoint.
- `validation-corpus.json` is the committed nine-document default corpus.

The validator runs documents sequentially because CPU pipeline models are the current verified environment and concurrent model initialization would distort resource measurements. `--repeat 2` performs a deterministic structured-output comparison without requiring byte-identical timestamps or logs. Failed documents remain in the report and do not prevent later documents from running. The CLI exits nonzero when an unexpected failure, unexpected success, package integrity error, or reproducibility mismatch occurs.

Each corpus entry declares `expected_status` as `completed` or `failed`. The default corpus expects all nine existing documents to complete. If a repository fixture later becomes intentionally invalid, its expectation must be changed explicitly rather than weakening global acceptance rules.

### Metrics and acceptance

The JSON and HTML reports include:

- completed, failed, expected-failure, and unexpected-failure counts;
- wall time and sampled peak RSS;
- source and package byte counts;
- block, table, page, and finding counts;
- table CSV fidelity distribution;
- unsupported block and native-verification finding counts;
- offline requested/verified values;
- reproducibility status for document IDs, block IDs, tree nodes, table IDs, structured JSONL, table files, and page filenames.

No extracted text, Markdown, table cell value, absolute source path, environment dump, or API credential is written to either report.

## Stage 2: External offline verification

### Control boundary

`verify_codex_export_offline.py` requires an elevated Windows session. It creates temporary outbound-block Windows Firewall rules for:

- the MinerU virtual-environment Python executable;
- the configured `uv.exe` launcher.

The script controls `netsh advfirewall` through argv lists with `shell=False`. It verifies that both rules are enabled with outbound block action, invokes the batch validator with local/offline model environment variables, writes a separate `offline-attestation.json`, and removes both rules in a `finally` block. The attestation records rule names, executable SHA-256 hashes, start/end UTC timestamps, validator exit code, report hash, and `verified=true` only when the rules were confirmed active for the complete successful validator interval.

The generated review-package manifests remain conservative with `offline.verified=false`; external proof lives in the attestation and batch report. This avoids allowing an ordinary CLI flag or environment variable to forge a package-level claim.

The script must fail before creating output when not elevated. The firewall controller accepts a subprocess executor so unit tests can verify exact `netsh` argv, privilege rejection, rule verification, and cleanup after both success and failure without changing host firewall state. Actual execution on the current host is attempted; lack of elevation is reported as an environmental blocker rather than silently weakening the control.

## Stage 3: Production PDF provider

### Configuration

Add:

```text
MINERU_PROVIDER=cloud|local
MINERU_LOCAL_SHARD_CONCURRENCY=1
```

`MINERU_PROVIDER` defaults to `cloud`. Invalid values fail configuration validation. Existing local project, uv, timeout, backend, method, language, offline, and log-limit settings remain the source of local runner configuration.

### Routing and materialization

`provider.py` exposes one production-facing function:

```python
def parse_pdf(
    pdf_path: str,
    filename: str,
    output_dir: str,
    *,
    s3_key: str | None = None,
) -> None:
    ...
```

When `MINERU_PROVIDER=cloud`, it delegates unchanged to the existing `parse_via_full()`. When set to `local`, it requires a local source path, invokes `LocalMinerURunner`, copies the validated Markdown artifact to `output_dir/full.md`, copies validated images into `output_dir/images/`, retains the sanitized local log, and removes temporary raw working artifacts after successful materialization. It never reads `MINERU_API_KEYS`, calls `requests`, or uses S3 for local parsing.

The standard and shard PDF paths call this dispatcher instead of importing the cloud service directly. Local shard concurrency uses `MINERU_LOCAL_SHARD_CONCURRENCY`, defaulting to one. Atlas routing and existing DOCX, PPTX, page-memory, and cloud behavior remain unchanged.

### Failure behavior

- Missing local configuration fails before starting parsing with an actionable message.
- Remote HTTP source URLs are rejected by the local provider.
- Local child timeout and nonzero exit retain their typed `LocalMinerUError` and sanitized log.
- Materialization uses a temporary destination and atomic rename for `full.md`; partial image copies are removed on failure.
- There is no automatic cloud fallback from local mode because that would violate operator intent and offline expectations.

## Testing strategy

All new Python behavior follows red-green TDD:

- corpus parsing and escape rejection;
- report privacy and deterministic ordering;
- continued execution after one document failure;
- package hash audit and reproducibility detection;
- cloud default delegation;
- local runner construction and artifact materialization;
- remote URL rejection and missing config errors;
- local shard concurrency selection;
- guarantee that the cloud client is never called in local mode.

Firewall behavior is tested through the injected subprocess executor. Existing standalone exporter, parse-task, PDF profile/anatomy, page-memory, and repository static checks remain regression gates.

## Security and operational constraints

- No customer or non-test documents are included automatically.
- Reports never contain extracted source content or absolute paths.
- Review packages remain ignored and are deleted after execution evidence is recorded.
- Firewall rules use unique names and are always removed in `finally`.
- The wrapper never disables an existing firewall profile or modifies unrelated rules.
- Production local mode is opt-in, has no silent cloud fallback, and starts with one concurrent model process.
- No push, pull request, merge, or default-branch modification is part of this work.

## Completion criteria

- The nine-document corpus is discovered safely and the batch report is generated.
- All runnable documents complete or are reported against explicit expectations.
- Report privacy, package integrity, and reproducibility contracts pass.
- The Firewall wrapper and tests are complete; an actual elevated run either produces a verified attestation or records the precise privilege blocker.
- Production cloud mode remains the default and its existing tests pass.
- Production local mode processes a synthetic PDF through the normal `parse_pdfs()` seam without a cloud request.
- Ruff, Pyright, focused tests, expanded regressions, and both Git audits pass.
