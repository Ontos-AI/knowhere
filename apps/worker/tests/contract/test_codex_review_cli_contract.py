from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_parser.providers.mineru.local_process import LocalMinerUError
from scripts import export_codex_review_package as cli


def test_cli_reports_local_mineru_failure_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "mineru.log"

    def fail_build(_request: Any) -> None:
        raise LocalMinerUError(
            "Local MinerU exited with return code 2.",
            return_code=2,
            timed_out=False,
            stderr_tail="offline model is missing",
            log_path=log_path,
        )

    monkeypatch.setattr(cli, "build_codex_review_package", fail_build)

    result = cli.main(
        [
            "--input",
            str(tmp_path / "input.pdf"),
            "--output",
            str(tmp_path / "package"),
            "--mineru-project",
            str(tmp_path / "MinerU"),
            "--lang",
            "en",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err == (
        "codex-review-export: Local MinerU exited with return code 2.\n"
    )
    assert "Traceback" not in captured.err
