"""Isolated local process runner for the MinerU Knowhere adapter."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.services.document_parser.providers.mineru.artifact_contract import (
    MinerUArtifactBundle,
    validate_mineru_artifact_bundle,
)


_SECRET_REPLACEMENTS = (
    (
        re.compile(
            r"(?i)\b(MINERU_API_KEYS?|API[_-]?KEY|TOKEN|PASSWORD)\b\s*[:=]\s*\S+"
        ),
        r"\1=<redacted>",
    ),
    (
        re.compile(r"(?i)\bAuthorization\s*:\s*Bearer\s+\S+"),
        "Authorization: Bearer <redacted>",
    ),
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer <redacted>"),
)


@dataclass(frozen=True)
class LocalMinerURequest:
    source_path: Path
    output_root: Path
    backend: str
    method: str
    language: str
    offline: bool


class LocalMinerUError(RuntimeError):
    """Typed failure from an isolated MinerU child process."""

    def __init__(
        self,
        message: str,
        *,
        return_code: int | None,
        timed_out: bool,
        stderr_tail: str,
        log_path: Path,
    ) -> None:
        super().__init__(message)
        self.return_code = return_code
        self.timed_out = timed_out
        self.stderr_tail = stderr_tail
        self.log_path = log_path


def _sanitize_log(text: str) -> str:
    sanitized = text
    for pattern, replacement in _SECRET_REPLACEMENTS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def _bound_log(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"<truncated>\n{text[-max_chars:]}"


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                shell=False,
                creationflags=creation_flags,
                timeout=10,
            )
            return
        except (OSError, subprocess.SubprocessError):
            process.kill()
            return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        process.kill()


class LocalMinerURunner:
    def __init__(
        self,
        project_path: Path,
        uv_executable: str,
        timeout_seconds: float,
        max_log_chars: int,
    ) -> None:
        project = Path(project_path).expanduser()
        if not project.is_absolute():
            raise ValueError("MinerU project path must be absolute.")
        project = project.resolve()
        if not project.is_dir():
            raise ValueError("MinerU project path does not exist.")
        if timeout_seconds <= 0:
            raise ValueError("MinerU timeout must be positive.")
        if max_log_chars <= 0:
            raise ValueError("MinerU max log characters must be positive.")

        executable = Path(uv_executable).expanduser()
        if executable.is_absolute():
            resolved_executable = executable.resolve()
            if not resolved_executable.is_file():
                raise ValueError("Configured uv executable does not exist.")
            executable_value = str(resolved_executable)
        else:
            located = shutil.which(uv_executable)
            if not located:
                raise ValueError("Configured uv executable was not found on PATH.")
            executable_value = str(Path(located).resolve())

        self.project_path = project
        self.uv_executable = executable_value
        self.timeout_seconds = timeout_seconds
        self.max_log_chars = max_log_chars

    def _build_argv(self, request: LocalMinerURequest) -> list[str]:
        argv = [
            self.uv_executable,
            "run",
            "--project",
            str(self.project_path),
            "mineru-knowhere-export",
            "--input",
            str(request.source_path.expanduser().resolve()),
            "--output",
            str(request.output_root.expanduser().resolve()),
            "--backend",
            request.backend,
            "--method",
            request.method,
            "--lang",
            request.language,
            "--offline" if request.offline else "--no-offline",
        ]
        return argv

    def run(self, request: LocalMinerURequest) -> MinerUArtifactBundle:
        source_path = request.source_path.expanduser().resolve()
        if not source_path.is_file():
            raise ValueError("Local MinerU source path must be an existing file.")
        output_root = request.output_root.expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        log_path = output_root.parent / "logs" / "mineru.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        process_options = {
            "cwd": str(self.project_path),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "shell": False,
        }
        if os.name == "nt":
            process_options["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            process_options["start_new_session"] = True

        process = subprocess.Popen(self._build_argv(request), **process_options)
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(process)
            stdout, stderr = process.communicate()

        sanitized_stdout = _sanitize_log(stdout or "")
        sanitized_stderr = _sanitize_log(stderr or "")
        log_content = (
            "[stdout]\n"
            f"{_bound_log(sanitized_stdout, self.max_log_chars)}\n"
            "[stderr]\n"
            f"{_bound_log(sanitized_stderr, self.max_log_chars)}\n"
        )
        log_path.write_text(log_content, encoding="utf-8")
        stderr_tail = sanitized_stderr[-self.max_log_chars :]

        if timed_out:
            raise LocalMinerUError(
                f"Local MinerU timed out after {self.timeout_seconds} seconds.",
                return_code=None,
                timed_out=True,
                stderr_tail=stderr_tail,
                log_path=log_path,
            )
        if process.returncode != 0:
            raise LocalMinerUError(
                f"Local MinerU exited with return code {process.returncode}.",
                return_code=process.returncode,
                timed_out=False,
                stderr_tail=stderr_tail,
                log_path=log_path,
            )

        return validate_mineru_artifact_bundle(
            manifest_path=output_root / "mineru_manifest.json",
            output_root=output_root,
            source_path=source_path,
        )
