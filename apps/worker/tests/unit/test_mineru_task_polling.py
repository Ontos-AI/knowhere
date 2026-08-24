from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_parser.providers.mineru import task_polling
from shared.core.exceptions.domain_exceptions import (
    MinerUTaskFailedException,
    PDFParsingException,
)


def _configure_poll_response(
    monkeypatch: pytest.MonkeyPatch,
    response_json: dict[str, object],
) -> None:
    lease = Mock(token_id="token-1", api_key="secret")
    quota_manager = Mock()
    quota_manager.acquire_request.return_value = lease
    response = Mock(status_code=200)
    response.json.return_value = response_json
    session = Mock()
    session.get.return_value = response

    monkeypatch.setattr(
        task_polling,
        "get_mineru_quota_manager",
        Mock(return_value=quota_manager),
    )
    monkeypatch.setattr(
        task_polling,
        "get_mineru_session",
        Mock(return_value=session),
    )


def test_failed_state_raises_mineru_task_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_poll_response(
        monkeypatch,
        {
            "code": 0,
            "data": {
                "extract_result": [
                    {
                        "state": "failed",
                        "err_msg": "parsing failed, please try again later",
                    }
                ]
            },
        },
    )

    with pytest.raises(MinerUTaskFailedException):
        task_polling.poll_mineru_task(
            status_url="https://mineru.example/status",
            task_id="batch-1",
            output_dir=str(tmp_path),
            get_status=task_polling.get_batch_status,
        )


def test_unexpected_status_error_is_not_mineru_task_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_poll_response(monkeypatch, {"code": 0, "data": {}})

    def raise_invalid_status(
        _response_json: dict[str, object],
    ) -> dict[str, object] | None:
        raise ValueError("invalid status payload")

    with pytest.raises(PDFParsingException) as exc_info:
        task_polling.poll_mineru_task(
            status_url="https://mineru.example/status",
            task_id="batch-1",
            output_dir=str(tmp_path),
            get_status=raise_invalid_status,
        )

    assert not isinstance(exc_info.value, MinerUTaskFailedException)
