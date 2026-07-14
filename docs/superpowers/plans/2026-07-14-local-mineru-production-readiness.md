# Local MinerU Production Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the opt-in local MinerU PDF provider safe to operate in a dedicated, concurrency-one production canary.

**Architecture:** A content-free runtime preflight validates the explicitly configured local installation before worker admission. A process-wide semaphore limits independent local jobs, while the provider boundary maps failures to safe domain exceptions and emits allowlisted structured observations. Deployment remains configuration-based: cloud is the default, local workers use a separate queue/deployment, and no application-level fallback or percentage router is added.

**Tech Stack:** Python 3.13, Pydantic settings, dataclasses, pathlib, psutil, threading/gevent-compatible semaphores, Loguru, pytest, uv, MinerU.

## Global Constraints

- Keep `MINERU_PROVIDER=cloud` as the default.
- Run preflight, capacity admission, and local-only configuration reads only when `MINERU_PROVIDER=local`.
- Never retry or fall back from local MinerU to cloud MinerU.
- Keep production DOCX routing unchanged.
- Keep `MINERU_LOCAL_SHARD_CONCURRENCY=1` and `MINERU_LOCAL_MAX_CONCURRENT_JOBS=1` for the initial canary.
- Every subprocess must use an argv list, `shell=False`, a bounded timeout, and application-offline environment variables.
- Status JSON and structured observations must not contain filenames, S3 keys, paths, document content, table values, environment values, credentials, subprocess output, or free-form exception messages.
- Do not change the anonymous telemetry schema in ADR-0004.
- Do not complete backlog item BL-001; the operator owns the elevated firewall attestation.
- Do not push, merge, create a pull request, or modify a default branch.

---

### Task 1: Local runtime preflight contract and CLI

**Files:**
- Create: `apps/worker/app/services/document_parser/providers/mineru/runtime_preflight.py`
- Create: `apps/worker/scripts/check_local_mineru_runtime.py`
- Create: `apps/worker/tests/contract/test_mineru_runtime_preflight_contract.py`
- Modify: `packages/shared-python/shared/core/config/mineru.py`
- Modify: `apps/worker/.env.example`

**Interfaces:**
- Produces: `LocalMinerURuntimeStatus`, `LocalMinerURuntimeError`, `check_local_mineru_runtime()`, and `require_local_mineru_runtime()`.
- Consumes: the combined `settings` object, injected command/capacity probes, and the exact local MinerU configuration.

- [ ] **Step 1: Write failing configuration and status tests**

Test cloud bypass, complete local readiness, missing project, missing `uv`, derived MinerU Python, failed adapter import, unwritable temp root, low disk, and low available memory. Assert serialization contains only this shape:

```python
{
    "schema_version": "local-mineru-runtime/1.0",
    "provider": "local",
    "ready": True,
    "checks": {
        "project": True,
        "uv": True,
        "python": True,
        "adapter": True,
        "temp_writable": True,
        "disk": True,
        "memory": True,
    },
    "free_disk_bytes": 20 * 1024**3,
    "available_memory_bytes": 16 * 1024**3,
    "error_codes": [],
}
```

Also assert the serialized payload excludes the configured project, executable, temp path, environment values, and fake subprocess output.

- [ ] **Step 2: Run the focused test and observe the missing module**

Run:

```powershell
python -m uv run pytest apps/worker/tests/contract/test_mineru_runtime_preflight_contract.py -q
```

Expected: collection fails because `runtime_preflight` does not exist.

- [ ] **Step 3: Add validated local readiness settings**

Add to `MineruConfig`:

```python
MINERU_LOCAL_PREFLIGHT_ON_STARTUP: bool = True
MINERU_LOCAL_PYTHON_EXECUTABLE: str = ""
MINERU_LOCAL_MAX_CONCURRENT_JOBS: int = Field(default=1, ge=1)
MINERU_LOCAL_ADMISSION_TIMEOUT_SECONDS: int = Field(default=30, gt=0)
MINERU_LOCAL_MIN_FREE_DISK_GB: int = Field(default=10, ge=1)
MINERU_LOCAL_MIN_AVAILABLE_MEMORY_GB: int = Field(default=8, ge=1)
```

Document the same defaults in `.env.example`. Do not read or validate these fields in cloud mode.

- [ ] **Step 4: Implement typed, content-free runtime checks**

Implement these contracts:

```python
@dataclass(frozen=True)
class LocalMinerURuntimeStatus:
    provider: str
    ready: bool
    checks: dict[str, bool]
    free_disk_bytes: int
    available_memory_bytes: int
    error_codes: tuple[str, ...]
    schema_version: str = "local-mineru-runtime/1.0"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "ready": self.ready,
            "checks": self.checks,
            "free_disk_bytes": self.free_disk_bytes,
            "available_memory_bytes": self.available_memory_bytes,
            "error_codes": list(self.error_codes),
        }

class LocalMinerURuntimeError(RuntimeError):
    def __init__(self, error_codes: tuple[str, ...]) -> None:
        self.error_codes = error_codes
        super().__init__("Local MinerU runtime is not ready: " + ",".join(error_codes))

def check_local_mineru_runtime(
    config: object,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
    virtual_memory: Callable[[], object] = psutil.virtual_memory,
    write_probe: Callable[[Path], bool] = _probe_writable,
) -> LocalMinerURuntimeStatus:
    if config.MINERU_PROVIDER != "local":
        return LocalMinerURuntimeStatus(
            provider="cloud", ready=True, checks={}, free_disk_bytes=0,
            available_memory_bytes=0, error_codes=(),
        )

    project_value = config.MINERU_LOCAL_PROJECT_PATH.strip()
    project = Path(project_value).expanduser().resolve() if project_value else Path()
    uv_executable = _resolve_uv(config.MINERU_LOCAL_UV_EXECUTABLE)
    mineru_python = _resolve_mineru_python(config, project)
    temp_root = Path(config.TMP_PATH).expanduser().resolve()
    try:
        free_disk_bytes = int(disk_usage(temp_root).free)
    except OSError:
        free_disk_bytes = 0
    try:
        available_memory_bytes = int(virtual_memory().available)
    except OSError:
        available_memory_bytes = 0
    checks = {
        "project": bool(project_value) and project.is_dir(),
        "uv": uv_executable is not None and uv_executable.is_file(),
        "python": mineru_python.is_file(),
        "adapter": False,
        "temp_writable": write_probe(temp_root),
        "disk": free_disk_bytes >= config.MINERU_LOCAL_MIN_FREE_DISK_GB * 1024**3,
        "memory": available_memory_bytes
        >= config.MINERU_LOCAL_MIN_AVAILABLE_MEMORY_GB * 1024**3,
    }
    if checks["uv"] and checks["python"]:
        checks["adapter"] = _probe_adapter(
            uv_executable, mineru_python, run_command=run_command
        )
    error_codes = tuple(
        code for check, code in _ERROR_CODES.items() if not checks[check]
    )
    return LocalMinerURuntimeStatus(
        provider="local", ready=not error_codes, checks=checks,
        free_disk_bytes=free_disk_bytes,
        available_memory_bytes=available_memory_bytes,
        error_codes=error_codes,
    )

def require_local_mineru_runtime(config: object) -> LocalMinerURuntimeStatus:
    status = check_local_mineru_runtime(config)
    if not status.ready:
        raise LocalMinerURuntimeError(status.error_codes)
    return status
```

Define every helper in the same module:

```python
_ERROR_CODES = {
    "project": "project_missing",
    "uv": "uv_missing",
    "python": "python_missing",
    "adapter": "adapter_unavailable",
    "temp_writable": "temp_not_writable",
    "disk": "disk_below_minimum",
    "memory": "memory_below_minimum",
}

def _resolve_uv(value: str) -> Path | None:
    configured = Path(value.strip()).expanduser()
    if configured.is_absolute():
        return configured.resolve()
    located = shutil.which(value.strip()) if value.strip() else None
    return Path(located).resolve() if located else None

def _resolve_mineru_python(config: object, project: Path) -> Path:
    configured = config.MINERU_LOCAL_PYTHON_EXECUTABLE.strip()
    if configured:
        return Path(configured).expanduser().resolve()
    relative = Path(".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    return (project / relative).resolve()

def _probe_writable(root: Path) -> bool:
    try:
        root.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=root, prefix=".mineru-preflight-")
        os.close(descriptor)
        Path(name).unlink(missing_ok=True)
        return True
    except OSError:
        return False

def _probe_adapter(
    uv_executable: Path,
    mineru_python: Path,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
) -> bool:
    environment = os.environ.copy()
    environment.update({
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "MODELSCOPE_OFFLINE": "1",
    })
    commands = (
        [str(uv_executable), "--version"],
        [str(mineru_python), "-I", "-c", "import mineru.integrations.knowhere.cli"],
    )
    try:
        return all(
            run_command(
                argv, check=False, capture_output=True, text=True,
                shell=False, timeout=10, env=environment,
            ).returncode == 0
            for argv in commands
        )
    except (OSError, subprocess.SubprocessError):
        return False
```

Resolve `MINERU_LOCAL_PYTHON_EXECUTABLE` explicitly when non-empty; otherwise derive `.venv/Scripts/python.exe` on Windows and `.venv/bin/python` elsewhere. Probe only:

```python
[uv_executable, "--version"]
[mineru_python, "-I", "-c", "import mineru.integrations.knowhere.cli"]
```

Use a ten-second timeout and `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and `MODELSCOPE_OFFLINE=1`. Reduce every failure to stable codes such as `project_missing`, `uv_missing`, `python_missing`, `adapter_unavailable`, `temp_not_writable`, `disk_below_minimum`, and `memory_below_minimum`.

- [ ] **Step 5: Implement the deployment preflight CLI**

The ignored script must be force-added to Git. It imports the shared settings, runs `check_local_mineru_runtime(settings)`, prints only `json.dumps(status.to_dict(), sort_keys=True)`, and exits 0 when ready or 1 when not ready. When the configured provider is cloud, return a ready status with `provider="cloud"` and no local subprocess calls.

- [ ] **Step 6: Verify and commit the preflight**

Run:

```powershell
python -m uv run pytest apps/worker/tests/contract/test_mineru_runtime_preflight_contract.py -q
python -m uv run ruff check apps/worker/app/services/document_parser/providers/mineru/runtime_preflight.py apps/worker/scripts/check_local_mineru_runtime.py apps/worker/tests/contract/test_mineru_runtime_preflight_contract.py packages/shared-python/shared/core/config/mineru.py
python -m uv run pyright apps/worker/app/services/document_parser/providers/mineru/runtime_preflight.py apps/worker/scripts/check_local_mineru_runtime.py packages/shared-python/shared/core/config/mineru.py
git add apps/worker/app/services/document_parser/providers/mineru/runtime_preflight.py apps/worker/tests/contract/test_mineru_runtime_preflight_contract.py packages/shared-python/shared/core/config/mineru.py apps/worker/.env.example
git add -f apps/worker/scripts/check_local_mineru_runtime.py
git commit -m "feat: add local MinerU runtime preflight"
```

### Task 2: Fail-fast worker startup

**Files:**
- Modify: `apps/worker/app/core/worker_bootstrap.py`
- Modify: `apps/worker/tests/contract/test_worker_bootstrap_contract.py`

**Interfaces:**
- Consumes: `settings.MINERU_PROVIDER`, `settings.MINERU_LOCAL_PREFLIGHT_ON_STARTUP`, and `require_local_mineru_runtime(settings)`.
- Produces: local-mode startup failure before the worker accepts jobs.

- [ ] **Step 1: Write failing bootstrap tests**

Add four named tests:

- `test_worker_startup_skips_mineru_preflight_in_cloud_mode`
- `test_worker_startup_requires_ready_local_mineru_when_enabled`
- `test_worker_startup_fails_before_heartbeat_for_invalid_local_runtime`
- `test_worker_startup_can_defer_preflight_when_operator_disables_it`

Patch logging, heartbeat, Redis, and the preflight function. Assert invalid explicit local configuration raises `LocalMinerURuntimeError` and does not call `start_worker_heartbeat()`.

- [ ] **Step 2: Run the focused tests and observe failure**

Run:

```powershell
python -m uv run pytest apps/worker/tests/contract/test_worker_bootstrap_contract.py -q
```

Expected: new assertions fail because `init_worker()` does not perform the preflight.

- [ ] **Step 3: Add preflight before worker side effects**

At the beginning of `init_worker()` after `setup_logging()` but before heartbeat/Redis initialization:

```python
if (
    settings.MINERU_PROVIDER == "local"
    and settings.MINERU_LOCAL_PREFLIGHT_ON_STARTUP
):
    status = require_local_mineru_runtime(settings)
    logger.bind(
        event="mineru.runtime_preflight",
        provider="local",
        ready=status.ready,
        free_disk_bytes=status.free_disk_bytes,
        available_memory_bytes=status.available_memory_bytes,
    ).info("Local MinerU runtime preflight passed")
```

Do not catch `LocalMinerURuntimeError`; explicit local misconfiguration must stop startup. Do not log `error_codes` until tests prove the fixed allowlist cannot expose free-form data.

- [ ] **Step 4: Verify and commit worker startup**

Run:

```powershell
$env:PYTEST_POSTGRESQL_EXECUTABLE='C:\Users\psc01\tools\postgresql-16.14\pgsql\bin\pg_ctl.exe'
python -m uv run pytest apps/worker/tests/contract/test_worker_bootstrap_contract.py apps/worker/tests/contract/test_mineru_runtime_preflight_contract.py -q
git add apps/worker/app/core/worker_bootstrap.py apps/worker/tests/contract/test_worker_bootstrap_contract.py
git commit -m "feat: fail fast on invalid local MinerU runtime"
```

### Task 3: Process-wide local job capacity guard

**Files:**
- Create: `apps/worker/app/services/document_parser/providers/mineru/local_capacity.py`
- Create: `apps/worker/tests/contract/test_mineru_local_capacity_contract.py`
- Modify: `apps/worker/app/services/document_parser/providers/mineru/local_pdf_service.py`

**Interfaces:**
- Produces: `LocalMinerUCapacityGuard`, `LocalMinerUCapacityError`, and `get_local_capacity_guard(limit, timeout_seconds)`.
- Consumes: local max-concurrency and admission-timeout settings.

- [ ] **Step 1: Write failing capacity tests**

Use real threads plus events for one strict admission test and injected semaphore objects for failure paths. Prove:

- one lease blocks a second lease when the limit is 1;
- wait timeout raises `LocalMinerUCapacityError` without calling the runner;
- leases release after success, `LocalMinerUError`, artifact error, and publication error;
- different repeated calls reuse the same guard for the same process/configuration;
- no source metadata is present in the capacity exception.

- [ ] **Step 2: Run the focused tests and observe the missing module**

Run:

```powershell
python -m uv run pytest apps/worker/tests/contract/test_mineru_local_capacity_contract.py -q
```

Expected: collection fails because `local_capacity` does not exist.

- [ ] **Step 3: Implement bounded admission**

Use this public shape:

```python
class LocalMinerUCapacityError(RuntimeError):
    pass

class LocalMinerUCapacityGuard:
    def __init__(self, limit: int, timeout_seconds: float) -> None:
        if limit < 1 or timeout_seconds <= 0:
            raise ValueError("Local MinerU capacity values must be positive")
        self.timeout_seconds = timeout_seconds
        self._semaphore = threading.BoundedSemaphore(limit)

    @contextmanager
    def acquire(self) -> Iterator[None]:
        acquired = self._semaphore.acquire(timeout=self.timeout_seconds)
        if not acquired:
            raise LocalMinerUCapacityError("Local MinerU capacity is unavailable")
        try:
            yield
        finally:
            self._semaphore.release()

_guard_lock = threading.Lock()
_guards: dict[tuple[int, float], LocalMinerUCapacityGuard] = {}

def get_local_capacity_guard(
    limit: int,
    timeout_seconds: float,
) -> LocalMinerUCapacityGuard:
    key = (limit, float(timeout_seconds))
    with _guard_lock:
        guard = _guards.get(key)
        if guard is None:
            guard = LocalMinerUCapacityGuard(limit, timeout_seconds)
            _guards[key] = guard
        return guard
```

Protect the process singleton/cache with a lock. Reject nonpositive values even if a non-Pydantic caller bypasses settings validation.

- [ ] **Step 4: Wrap the complete local run and publication transaction**

In `parse_via_local()`, acquire the guard after validating source/configuration and before creating `.mineru-local-*`. Hold it through runner execution, artifact copying, atomic publication, and temporary cleanup. This ensures capacity is not released while peak disk usage still exists.

- [ ] **Step 5: Verify and commit capacity control**

Run:

```powershell
python -m uv run pytest apps/worker/tests/contract/test_mineru_local_capacity_contract.py apps/worker/tests/contract/test_mineru_provider_contract.py apps/worker/tests/contract/test_local_mineru_process_contract.py -q
python -m uv run ruff check apps/worker/app/services/document_parser/providers/mineru/local_capacity.py apps/worker/app/services/document_parser/providers/mineru/local_pdf_service.py apps/worker/tests/contract/test_mineru_local_capacity_contract.py
git add apps/worker/app/services/document_parser/providers/mineru/local_capacity.py apps/worker/app/services/document_parser/providers/mineru/local_pdf_service.py apps/worker/tests/contract/test_mineru_local_capacity_contract.py
git commit -m "feat: bound concurrent local MinerU jobs"
```

### Task 4: Safe provider errors and structured observations

**Files:**
- Modify: `apps/worker/app/services/document_parser/providers/mineru/provider.py`
- Modify: `apps/worker/tests/contract/test_mineru_provider_contract.py`

**Interfaces:**
- Consumes: cloud/local provider functions, `LocalMinerUCapacityError`, `LocalMinerUError`, `MinerUArtifactContractError`, and `MinerUServiceException`.
- Produces: exactly one allowlisted observation per provider call and safe local domain failures.

- [ ] **Step 1: Write failing error and log contracts**

Capture Loguru records and assert successful cloud/local observations contain only:

```python
{
    "event": "mineru.provider",
    "provider": "local",
    "backend": "pipeline",
    "status": "ok",
    "elapsed_ms": 123,
}
```

For failures, allow only `error_category` from:

```python
{
    "capacity",
    "configuration",
    "process_timeout",
    "process_exit",
    "artifact_contract",
    "publication",
    "unknown",
}
```

Inject paths, filenames, S3 keys, API keys, and child stderr into exceptions and prove none appear in bound record fields, rendered log text, or `MinerUServiceException.details/user_message`. Prove local failure calls cloud zero times.

- [ ] **Step 2: Run the focused tests and observe failure**

Run:

```powershell
python -m uv run pytest apps/worker/tests/contract/test_mineru_provider_contract.py -q
```

Expected: observation assertions and safe domain-exception assertions fail.

- [ ] **Step 3: Add stable categorization and provider timing**

Implement private helpers with exhaustive typed branches:

```python
def _local_error_category(error: Exception) -> str:
    if isinstance(error, LocalMinerUCapacityError):
        return "capacity"
    if isinstance(error, MinerUArtifactContractError):
        return "artifact_contract"
    if isinstance(error, LocalMinerUError):
        return "process_timeout" if error.timed_out else "process_exit"
    if isinstance(error, (ValueError, LocalMinerURuntimeError)):
        return "configuration"
    if isinstance(error, OSError):
        return "publication"
    return "unknown"
```

Measure with `time.perf_counter()`. Bind only `event`, `provider`, `backend`, `status`, `elapsed_ms`, and optional stable `error_category`. On local failure raise:

```python
raise MinerUServiceException(
    internal_message=f"Local MinerU provider failed: {category}",
    original_exception=error,
) from error
```

Cloud exceptions retain their current type and behavior after the error observation; never remap them through the local boundary.

- [ ] **Step 4: Verify and commit the error boundary**

Run:

```powershell
python -m uv run pytest apps/worker/tests/contract/test_mineru_provider_contract.py apps/worker/tests/contract/test_parse_task_contract.py apps/worker/tests/contract/test_doc_profile_anatomy_contract.py -q
python -m uv run ruff check apps/worker/app/services/document_parser/providers/mineru/provider.py apps/worker/tests/contract/test_mineru_provider_contract.py
git add apps/worker/app/services/document_parser/providers/mineru/provider.py apps/worker/tests/contract/test_mineru_provider_contract.py
git commit -m "feat: add safe local MinerU provider observations"
```

### Task 5: Canary corpus, real seam smoke, and operations runbook

**Files:**
- Create: `apps/worker/tests/fixtures/codex_export/local-mineru-canary-corpus.json`
- Create: `apps/worker/tests/integration/test_local_mineru_provider_integration.py`
- Create: `docs/guides/local-mineru-production-canary.md`
- Modify: `docs/guides/codex-review-package.md`

**Interfaces:**
- Consumes: the preflight CLI, provider seam, existing validation corpus loader/runner, and public MinerU demo documents.
- Produces: a three-document repeatable canary and exact operator rollout/rollback gates.

- [ ] **Step 1: Add the three-document canary corpus**

Use only these already committed public/test documents:

```json
{
  "schema_version": "codex-validation-corpus/1.0",
  "documents": [
    {"id":"canary-unit-pdf","root":"mineru","path":"tests/unittest/pdfs/test.pdf","tags":["pdf","smoke"],"language":"en","pages":[1],"expected_status":"completed"},
    {"id":"canary-small-ocr","root":"mineru","path":"demo/pdfs/small_ocr.pdf","tags":["pdf","ocr"],"language":"en","pages":[1,2],"expected_status":"completed"},
    {"id":"canary-table-pdf","root":"mineru","path":"demo/pdfs/demo2.pdf","tags":["pdf","table"],"language":"en","pages":[1,2],"expected_status":"completed"}
  ]
}
```

- [ ] **Step 2: Write an opt-in real provider seam test**

Guard with `RUN_LOCAL_MINERU_E2E=1`. Require `MINERU_LOCAL_PROJECT_PATH` and `MINERU_LOCAL_UV_EXECUTABLE`. Set provider/local concurrency settings through monkeypatch, replace `provider.parse_via_full` with a sentinel that fails, replace only downstream `parse_md`, invoke standard `parse_pdfs()`, and assert `full.md`, sanitized log, no `.mineru-local-*`, and no cloud call.

- [ ] **Step 3: Write the dedicated-worker canary runbook**

Document exact stages:

1. Preflight CLI must return `ready=true`.
2. Run the three-document corpus with `--repeat 3`, application offline, CPU pipeline, and concurrency 1.
3. Require 9/9 completion, zero expectation/reproducibility failures, no temporary output, and peak RSS below the deployment memory limit.
4. Start a dedicated local worker queue/deployment with worker concurrency 1; route only approved 1–20 page non-confidential PDFs.
5. Observe for 24 hours; require zero unexpected failures and p95 extraction duration below `MINERU_LOCAL_TIMEOUT_SECONDS`.
6. Advance volume manually or rollback by draining the local queue and routing new jobs to a cloud-configured worker.

Explicitly prohibit changing `MINERU_PROVIDER` in place on a busy worker, automatic retry to cloud, customer-wide rollout, DOCX routing, and concurrency increases.

- [ ] **Step 4: Verify docs, fixture loading, and opt-in skip**

Run:

```powershell
python -m uv run pytest apps/worker/tests/contract/test_codex_batch_validation_contract.py apps/worker/tests/integration/test_local_mineru_provider_integration.py -q
git diff --check
git add apps/worker/tests/fixtures/codex_export/local-mineru-canary-corpus.json apps/worker/tests/integration/test_local_mineru_provider_integration.py docs/guides/local-mineru-production-canary.md docs/guides/codex-review-package.md
git commit -m "docs: add local MinerU production canary runbook"
```

### Task 6: Execute readiness validation and close the phase

**Files:**
- Modify: `docs/superpowers/plans/2026-07-14-local-mineru-production-readiness.md`

**Interfaces:**
- Consumes: all prior tasks and the paired MinerU checkout.
- Produces: recorded readiness evidence with generated artifacts removed.

- [ ] **Step 1: Run the actual content-free preflight**

Set local provider/project/executable values only in the current process and run `check_local_mineru_runtime.py`. Record ready state, free disk bytes, available memory bytes, and check booleans; do not record paths or environment values.

- [ ] **Step 2: Run the repeat-three public canary**

Run the new three-document corpus into ignored `.codex-review/production-canary`. Acceptance is 9 completed, 0 failed, 0 expectation mismatches, 0 reproducibility failures, max sampled RSS below available-memory admission, and no `.building-*`, `.work-*`, `.failed-*`, or `.mineru-local-*` directory after completion.

- [ ] **Step 3: Run the real provider seam E2E**

Run:

```powershell
$env:RUN_LOCAL_MINERU_E2E='1'
$env:MINERU_LOCAL_PROJECT_PATH='C:\Users\psc01\workspace\MinerU'
$env:MINERU_LOCAL_UV_EXECUTABLE='C:\Users\psc01\AppData\Roaming\Python\Python313\Scripts\uv.exe'
python -m uv run pytest apps/worker/tests/integration/test_local_mineru_provider_integration.py -q
```

Expected: 1 passed and the cloud sentinel remains uncalled.

- [ ] **Step 4: Run full verification**

Run:

```powershell
$env:PYTEST_POSTGRESQL_EXECUTABLE='C:\Users\psc01\tools\postgresql-16.14\pgsql\bin\pg_ctl.exe'
python -m uv run pytest apps/worker/tests/contract -q
python -m uv run --all-packages --group lint ruff check apps packages
python -m uv run --all-packages --group typecheck pyright --project pyproject.toml apps/api/app apps/api/main.py apps/worker/app apps/worker/worker.py packages/shared-python/shared
C:\Users\psc01\workspace\MinerU\.venv\Scripts\python.exe -m pytest C:\Users\psc01\workspace\MinerU\tests\unittest\test_knowhere_integration_contract.py -q
```

- [ ] **Step 5: Record evidence, clean, and commit**

Append exact preflight, canary, E2E, test, Ruff, and Pyright results to this plan. Remove `.codex-review/production-canary`, coverage output, pytest caches created by this phase, and an untracked MinerU `uv.lock`. Confirm zero `Knowhere-MinerU-Offline-*` rules even though BL-001 was not run.

```powershell
git diff --check
git status --short
git -C C:\Users\psc01\workspace\MinerU status --short
git add docs/superpowers/plans/2026-07-14-local-mineru-production-readiness.md
git commit -m "docs: record local MinerU readiness validation"
```
