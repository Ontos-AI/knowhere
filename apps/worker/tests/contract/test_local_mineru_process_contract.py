from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.services.document_parser.providers.mineru.artifact_contract import (
    MinerUArtifactBundle,
    MinerUArtifactManifest,
)
from app.services.document_parser.providers.mineru.local_process import (
    LocalMinerURequest,
)


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        timeout_once: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timeout_once = timeout_once
        self.pid = 4242
        self.communicate_calls = 0

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_calls += 1
        if self.timeout_once and self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(cmd="mineru", timeout=timeout or 0)
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.returncode = -9


def _request(tmp_path: Path) -> LocalMinerURequest:
    source = tmp_path / "source files" / "report.pdf"
    source.parent.mkdir()
    source.write_bytes(b"%PDF-1.7 synthetic")
    return LocalMinerURequest(
        source_path=source,
        output_root=tmp_path / "job output" / "mineru",
        backend="pipeline",
        method="auto",
        language="en",
        offline=True,
    )


def _bundle(request: LocalMinerURequest) -> MinerUArtifactBundle:
    manifest = MinerUArtifactManifest(
        schema_version="knowhere-mineru-artifacts/1.0",
        status="completed",
        source={},
        parser={},
        execution={},
        document={},
        artifacts={},
        warnings=(),
        raw={},
    )
    root = request.output_root
    return MinerUArtifactBundle(
        manifest_path=root / "mineru_manifest.json",
        output_root=root,
        markdown_path=root / "report.md",
        middle_json_path=root / "report_middle.json",
        content_list_path=root / "report_content_list.json",
        content_list_v2_path=root / "report_content_list_v2.json",
        images_dir=root / "images",
        manifest=manifest,
    )


def test_runner_builds_argv_list_for_paths_with_spaces_and_uses_no_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    project_path = tmp_path / "MinerU project"
    project_path.mkdir()
    uv_path = tmp_path / "tools" / "uv.exe"
    uv_path.parent.mkdir()
    uv_path.touch()
    process = _FakeProcess(stdout=str(request.output_root / "mineru_manifest.json"))
    seen: dict[str, Any] = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return process

    expected_bundle = _bundle(request)
    from app.services.document_parser.providers.mineru import local_process

    monkeypatch.setattr(local_process.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        local_process,
        "validate_mineru_artifact_bundle",
        lambda **_kwargs: expected_bundle,
    )
    runner = local_process.LocalMinerURunner(
        project_path=project_path,
        uv_executable=str(uv_path),
        timeout_seconds=30,
        max_log_chars=1000,
    )

    result = runner.run(request)

    assert result is expected_bundle
    assert seen["argv"] == [
        str(uv_path.resolve()),
        "run",
        "--project",
        str(project_path.resolve()),
        "mineru-knowhere-export",
        "--input",
        str(request.source_path.resolve()),
        "--output",
        str(request.output_root.resolve()),
        "--backend",
        "pipeline",
        "--method",
        "auto",
        "--lang",
        "en",
        "--offline",
    ]
    assert seen["kwargs"]["shell"] is False


def test_nonzero_exit_raises_typed_error_and_redacts_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    project_path = tmp_path / "MinerU"
    project_path.mkdir()
    uv_path = tmp_path / "uv.exe"
    uv_path.touch()
    process = _FakeProcess(
        stderr=(
            "MINERU_API_KEYS=super-secret Authorization: Bearer token-value\n"
            "Bearer bare-token"
        ),
        returncode=7,
    )
    from app.services.document_parser.providers.mineru import local_process

    monkeypatch.setattr(local_process.subprocess, "Popen", lambda *_a, **_k: process)
    runner = local_process.LocalMinerURunner(project_path, str(uv_path), 30, 1000)

    with pytest.raises(local_process.LocalMinerUError) as captured:
        runner.run(request)

    assert captured.value.return_code == 7
    assert captured.value.timed_out is False
    assert "super-secret" not in captured.value.stderr_tail
    assert "token-value" not in captured.value.stderr_tail
    assert "bare-token" not in captured.value.stderr_tail
    assert captured.value.log_path.is_file()


def test_timeout_terminates_process_tree_and_surfaces_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    project_path = tmp_path / "MinerU"
    project_path.mkdir()
    uv_path = tmp_path / "uv.exe"
    uv_path.touch()
    process = _FakeProcess(stderr="timed out", timeout_once=True)
    terminated: list[int] = []
    from app.services.document_parser.providers.mineru import local_process

    monkeypatch.setattr(local_process.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(
        local_process,
        "_terminate_process_tree",
        lambda child: terminated.append(child.pid),
    )
    runner = local_process.LocalMinerURunner(project_path, str(uv_path), 0.01, 1000)

    with pytest.raises(local_process.LocalMinerUError) as captured:
        runner.run(request)

    assert terminated == [4242]
    assert captured.value.timed_out is True
    assert captured.value.return_code is None


def test_runner_bounds_persisted_stdout_and_stderr_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    project_path = tmp_path / "MinerU"
    project_path.mkdir()
    uv_path = tmp_path / "uv.exe"
    uv_path.touch()
    process = _FakeProcess(stdout="o" * 5000, stderr="e" * 5000, returncode=1)
    from app.services.document_parser.providers.mineru import local_process

    monkeypatch.setattr(local_process.subprocess, "Popen", lambda *_a, **_k: process)
    runner = local_process.LocalMinerURunner(project_path, str(uv_path), 30, 80)

    with pytest.raises(local_process.LocalMinerUError) as captured:
        runner.run(request)

    log_content = captured.value.log_path.read_text(encoding="utf-8")
    assert len(log_content) < 300
    assert "<truncated>" in log_content


def test_runner_rejects_non_absolute_or_missing_project_path(tmp_path: Path) -> None:
    from app.services.document_parser.providers.mineru import local_process

    with pytest.raises(ValueError, match="absolute"):
        local_process.LocalMinerURunner(Path("relative/MinerU"), "uv", 30, 1000)

    with pytest.raises(ValueError, match="does not exist"):
        local_process.LocalMinerURunner(tmp_path / "missing", "uv", 30, 1000)


def test_existing_cloud_mineru_entrypoint_remains_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test"
    )
    monkeypatch.setenv("TMP_PATH", "/tmp/knowhere-test")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-uploads")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_TEMP_PATH", "/tmp")
    from app.services.document_parser.providers.mineru.pdf_service import parse_via_full

    assert callable(parse_via_full)
