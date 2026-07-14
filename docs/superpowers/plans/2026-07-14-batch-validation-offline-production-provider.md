# Batch Validation, Offline Verification, and Production Local Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and execute a privacy-safe nine-document batch validator, an externally enforced Windows Firewall offline verifier, and an opt-in production local MinerU PDF provider that preserves the current cloud default.

**Architecture:** The batch layer reuses the standalone review-package builder and produces content-free JSON/HTML metrics. The offline layer wraps that CLI with temporary per-program outbound firewall rules. The production layer adds a small cloud/local dispatcher at the existing `parse_via_full()` seam and materializes local artifacts into the same `full.md` plus `images/` shape consumed downstream.

**Tech Stack:** Python 3.13, dataclasses, pathlib, psutil, pytest, uv, MinerU, Windows `netsh advfirewall`, HTML5, JSON, Pydantic settings.

## Global Constraints

- Use only files already committed under `tests/`, `demo/`, or `fixtures/` in Knowhere and MinerU.
- Never include extracted content, table cell values, absolute paths, environment dumps, or credentials in batch reports.
- Resolve corpus paths beneath the declared Knowhere or MinerU root and reject absolute, traversal, and symlink escapes.
- Run batch documents sequentially and default production local shard concurrency to one.
- Keep `MINERU_PROVIDER=cloud` as the default and never silently fall back from local to cloud.
- Keep DOCX production parsing unchanged; production provider routing applies only to PDF MinerU calls.
- Keep package `offline.verified=false`; external verification is represented by a separate attestation.
- Use argv lists and `shell=False` for every child process.
- Remove temporary firewall rules in `finally` and never modify unrelated rules or firewall profiles.
- Do not push, merge, open a pull request, or modify a default branch.

---

### Task 1: Safe corpus contract

**Files:**
- Create: `apps/worker/app/services/codex_export/validation_corpus.py`
- Create: `apps/worker/tests/contract/test_codex_batch_validation_contract.py`
- Create: `apps/worker/tests/fixtures/codex_export/validation-corpus.json`

**Interfaces:**
- Produces: `ValidationCorpus`, `ValidationDocument`, `load_validation_corpus(path, roots)`.
- Consumes: named root paths supplied by the CLI; no global settings.

- [ ] **Step 1: Write failing corpus tests**

Cover a valid relative document, duplicate ID, absolute path, `..`, unsupported suffix, missing file, and symlink escape. Assert the loaded record contains only `document_id`, `root_name`, relative `path`, `tags`, `language`, `pages`, and `expected_status`.

- [ ] **Step 2: Run the focused test and observe failure**

Run:

```powershell
python -m uv run pytest apps/worker/tests/contract/test_codex_batch_validation_contract.py -q
```

Expected: collection fails because `validation_corpus` does not exist.

- [ ] **Step 3: Implement the corpus contract**

Use these signatures:

```python
@dataclass(frozen=True)
class ValidationDocument:
    document_id: str
    root_name: str
    relative_path: PurePosixPath
    tags: tuple[str, ...]
    language: str
    pages: tuple[int, ...]
    expected_status: Literal["completed", "failed"]
    source_path: Path = field(repr=False)

@dataclass(frozen=True)
class ValidationCorpus:
    schema_version: str
    documents: tuple[ValidationDocument, ...]

```

The loader interface is `load_validation_corpus(corpus_path: Path, *, roots: Mapping[str, Path]) -> ValidationCorpus`.

Validate schema version `codex-validation-corpus/1.0`; require IDs matching `[a-z0-9][a-z0-9-]{0,63}`; require suffix `.pdf` or `.docx`; resolve paths and call `relative_to(root)` after resolution.

- [ ] **Step 4: Add the nine-document default corpus**

Use the exact paths listed in the design spec, tagged by `pdf`, `docx`, `ocr`, `english`, `chinese`, `table`, and `multi-page` where the existing filename or fixture role establishes that property. Request pages `[1]` for one-page files and `[1,2]` for multi-page files.

- [ ] **Step 5: Run the focused tests and commit**

Expected: corpus tests pass.

```powershell
git add apps/worker/app/services/codex_export/validation_corpus.py apps/worker/tests/contract/test_codex_batch_validation_contract.py apps/worker/tests/fixtures/codex_export/validation-corpus.json
git commit -m "feat: add safe Codex validation corpus"
```

### Task 2: Batch runner and privacy-safe reports

**Files:**
- Create: `apps/worker/app/services/codex_export/validation_runner.py`
- Create: `apps/worker/app/services/codex_export/validation_report.py`
- Create: `apps/worker/scripts/validate_codex_export_corpus.py`
- Modify: `apps/worker/tests/contract/test_codex_batch_validation_contract.py`

**Interfaces:**
- Consumes: `ValidationCorpus`, `ReviewPackageRequest`, `build_codex_review_package()`.
- Produces: `ValidationRunResult`, `ValidationReport`, `run_validation_corpus()`, `write_validation_reports()`.

- [ ] **Step 1: Write failing runner and report tests**

Use a fake package builder to prove sequential execution, continuation after one failure, expectation accounting, deterministic ordering, repeat comparison, artifact hash validation, table fidelity aggregation, and absence of source text/absolute paths/secrets in JSON and HTML.

- [ ] **Step 2: Run the focused tests and observe failure**

Expected: imports for runner and report modules fail.

- [ ] **Step 3: Implement result contracts and package audit**

Use serializable dataclasses. Result records may contain only source metadata, SHA-256, byte counts, duration, sampled peak RSS, package counts, fidelity/finding counters, expected/actual status, reproducibility status, and sanitized error type/message. Audit every artifact inventory entry before counting a package as completed.

- [ ] **Step 4: Implement sequential execution and resource sampling**

Use a daemon sampling thread around each package build. Sum RSS for the current process and recursive children through `psutil.Process`; tolerate process exit races. Use `time.perf_counter()` and always stop/join the monitor in `finally`.

- [ ] **Step 5: Implement atomic JSON and escaped HTML output**

Write `validation-report.json` and `validation-report.html` through sibling temporary files plus `os.replace()`. Generate HTML with `html.escape()` and no script or external asset dependency.

- [ ] **Step 6: Implement the CLI**

Required options:

```text
--corpus
--output
--mineru-project
--repeat (default 1)
--backend (default pipeline)
--method (default auto)
--dpi (default 144)
--offline / --no-offline (default offline)
--force
```

The CLI maps `knowhere` to the repository root and `mineru` to `--mineru-project`, exits zero only when actual statuses match expectations and reproducibility audits pass, and prints only the two report paths plus summary counts.

- [ ] **Step 7: Run focused tests and commit**

```powershell
python -m uv run pytest apps/worker/tests/contract/test_codex_batch_validation_contract.py -q
python -m uv run ruff check apps/worker/app/services/codex_export/validation_*.py apps/worker/scripts/validate_codex_export_corpus.py apps/worker/tests/contract/test_codex_batch_validation_contract.py
git add apps/worker/app/services/codex_export/validation_runner.py apps/worker/app/services/codex_export/validation_report.py apps/worker/scripts/validate_codex_export_corpus.py apps/worker/tests/contract/test_codex_batch_validation_contract.py
git commit -m "feat: add batch Codex export validation reports"
```

### Task 3: Windows Firewall offline attestation

**Files:**
- Create: `apps/worker/app/services/codex_export/offline_verifier.py`
- Create: `apps/worker/scripts/verify_codex_export_offline.py`
- Create: `apps/worker/tests/contract/test_codex_offline_verification_contract.py`

**Interfaces:**
- Produces: `WindowsFirewallController`, `OfflineVerificationRequest`, `verify_offline_validation()`.
- Consumes: batch-validator argv and exact `uv.exe`/MinerU `.venv/Scripts/python.exe` paths.

- [ ] **Step 1: Write failing offline-controller tests**

Inject a fake `run_command(argv, **kwargs)` callable. Assert exact `netsh advfirewall firewall add rule`, `show rule`, and `delete rule` argv; non-admin rejection before rule creation; both-rule active verification; validator environment; cleanup after validator success, failure, and exception; attestation remains `verified=false` on every failure path.

- [ ] **Step 2: Run focused tests and observe failure**

Expected: `offline_verifier` import fails.

- [ ] **Step 3: Implement the controller and attestation**

Use unique rule names prefixed `Knowhere-MinerU-Offline-`. Check elevation with `ctypes.windll.shell32.IsUserAnAdmin()`. Hash both executables and the final JSON report. The attestation schema is `codex-offline-attestation/1.0`; write it atomically. Delete each known rule in `finally`, even when the matching add command failed.

- [ ] **Step 4: Implement the CLI and execute the non-admin preflight**

The CLI accepts the batch-validator arguments plus `--uv-executable`, `--mineru-python`, and `--attestation`. On this host it must exit nonzero with `Administrator privileges are required` before creating a firewall rule or attestation.

- [ ] **Step 5: Run tests and commit**

```powershell
python -m uv run pytest apps/worker/tests/contract/test_codex_offline_verification_contract.py -q
git add apps/worker/app/services/codex_export/offline_verifier.py apps/worker/scripts/verify_codex_export_offline.py apps/worker/tests/contract/test_codex_offline_verification_contract.py
git commit -m "feat: add external offline validation attestation"
```

### Task 4: Feature-flagged production provider dispatcher

**Files:**
- Create: `apps/worker/app/services/document_parser/providers/mineru/provider.py`
- Create: `apps/worker/app/services/document_parser/providers/mineru/local_pdf_service.py`
- Create: `apps/worker/tests/contract/test_mineru_provider_contract.py`
- Modify: `packages/shared-python/shared/core/config/mineru.py`
- Modify: `apps/worker/app/services/document_parser/formats/pdf/parser.py`
- Modify: `apps/worker/.env.example`

**Interfaces:**
- Produces: `parse_pdf(pdf_path, filename, output_dir, *, s3_key=None)`.
- Consumes: cloud `parse_via_full()`, `LocalMinerURunner`, validated artifact bundle.

- [ ] **Step 1: Write failing provider tests**

Assert cloud is the default and delegates every argument; local mode never calls cloud; missing project/uv configuration fails clearly; remote sources fail; local Markdown is atomically materialized as `full.md`; images are copied without traversal; local raw work is removed after success; failure removes partial outputs; local shard concurrency defaults to one.

- [ ] **Step 2: Run focused tests and observe failure**

```powershell
python -m uv run pytest apps/worker/tests/contract/test_mineru_provider_contract.py -q
```

Expected: provider modules do not exist or configuration fields are absent.

- [ ] **Step 3: Add validated configuration**

Add:

```python
MINERU_PROVIDER: Literal["cloud", "local"] = "cloud"
MINERU_LOCAL_SHARD_CONCURRENCY: int = Field(default=1, ge=1)
```

Document both values in `.env.example` while keeping cloud as the default.

- [ ] **Step 4: Implement local materialization**

Run MinerU into a temporary directory under `output_dir`. Atomically copy Markdown to `full.md`; copy only regular files that resolve beneath the validated images directory; retain the sanitized log at `output_dir/logs/mineru.log`; remove raw temp data on success. Do not access S3, API keys, or requests.

- [ ] **Step 5: Implement dispatcher and change PDF call sites**

Replace direct `parse_via_full` imports/calls in `formats/pdf/parser.py` with `parse_pdf`. In shard parsing select `settings.MINERU_LOCAL_SHARD_CONCURRENCY` only when provider is local; otherwise retain `settings.MINERU_SHARD_CONCURRENCY`.

- [ ] **Step 6: Run focused and affected tests, then commit**

```powershell
python -m uv run pytest apps/worker/tests/contract/test_mineru_provider_contract.py apps/worker/tests/contract/test_local_mineru_process_contract.py apps/worker/tests/contract/test_doc_profile_anatomy_contract.py apps/worker/tests/contract/test_parse_task_contract.py -q
git add packages/shared-python/shared/core/config/mineru.py apps/worker/.env.example apps/worker/app/services/document_parser/providers/mineru/provider.py apps/worker/app/services/document_parser/providers/mineru/local_pdf_service.py apps/worker/app/services/document_parser/formats/pdf/parser.py apps/worker/tests/contract/test_mineru_provider_contract.py
git commit -m "feat: add production local MinerU provider"
```

### Task 5: Documentation and actual execution

**Files:**
- Modify: `docs/guides/codex-review-package.md`
- Modify: `docs/superpowers/plans/2026-07-14-batch-validation-offline-production-provider.md`

- [ ] **Step 1: Document batch, offline, and provider commands**

Include report privacy guarantees, corpus root rules, non-admin behavior, external attestation semantics, feature flag rollout, no-fallback policy, and concurrency one.

- [ ] **Step 2: Execute the nine-document batch**

Run with `--repeat 1`, CPU, local model source, and offline application flags. Save output under ignored `.codex-review/batch-validation`. Record exact success/failure counts, wall times, peak RSS, table fidelity, and findings in this plan's execution report.

- [ ] **Step 3: Execute the Firewall verifier**

Attempt the real command. If the current token is not elevated, record the exact exit code/message and absence of any `Knowhere-MinerU-Offline-*` rules as the priority-2 environmental blocker.

- [ ] **Step 4: Execute production local-provider integration**

Use the synthetic PDF generator, set `MINERU_PROVIDER=local`, and invoke `parse_pdfs()` with title-LLM behavior stubbed only where required to isolate the provider seam. Assert `full.md` exists and no cloud session method is called.

- [ ] **Step 5: Update execution report and commit docs**

```powershell
git add docs/guides/codex-review-package.md docs/superpowers/plans/2026-07-14-batch-validation-offline-production-provider.md
git commit -m "docs: record local provider validation results"
```

### Task 6: Final verification and cleanup

**Files:**
- Remove generated `.codex-review/batch-validation/`, synthetic sources, caches, coverage, and untracked MinerU `uv.lock`.

- [ ] **Step 1: Run targeted tests and repository checks**

```powershell
python -m uv run pytest apps/worker/tests/contract/test_codex_batch_validation_contract.py apps/worker/tests/contract/test_codex_offline_verification_contract.py apps/worker/tests/contract/test_mineru_provider_contract.py apps/worker/tests/contract/test_local_mineru_process_contract.py apps/worker/tests/contract/test_codex_review_package_contract.py -q
make check
```

- [ ] **Step 2: Run expanded worker regressions**

Run parse-task, PDF anatomy, DOCX, page-memory, and telemetry contracts with portable PostgreSQL 16.

- [ ] **Step 3: Clean and audit both repositories**

Expected: both `git status --short` outputs are empty; `git diff --check` passes; no generated document, package, firewall rule, `.env`, credential, coverage output, model, or untracked lockfile remains.
