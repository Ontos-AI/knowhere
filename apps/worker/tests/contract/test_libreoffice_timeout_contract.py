from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.services.document_parser.conversion import legacy_converter

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import LibreOfficeServiceException


def test_conversion_timeout_stops_the_whole_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "LIBREOFFICE_CONVERSION_TIMEOUT_SECONDS", 1)
    child_pid_file = tmp_path / "child.pid"
    # Like the soffice launcher, the shell starts a long-running child.
    command = ["sh", "-c", f"sleep 30 & echo $! > {child_pid_file}; wait"]

    started = time.monotonic()
    with pytest.raises(LibreOfficeServiceException, match="timed out after 1s"):
        legacy_converter._run_libreoffice(
            command, source_path="deck.pptx", operation="convert_pptx_to_pdf"
        )

    assert time.monotonic() - started < 10
    child_pid = int(child_pid_file.read_text().strip())
    with pytest.raises(ProcessLookupError):
        for _ in range(50):
            os.kill(child_pid, 0)
            time.sleep(0.1)


def test_conversion_within_timeout_returns_its_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LIBREOFFICE_CONVERSION_TIMEOUT_SECONDS", 5)

    result = legacy_converter._run_libreoffice(
        ["sh", "-c", "echo converted"], source_path="deck.pptx", operation="convert_pptx_to_pdf"
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "converted"
