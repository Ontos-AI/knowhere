from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock, call

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_parser.providers.mineru import pdf_service
from shared.core.exceptions.domain_exceptions import (
    MinerUTaskFailedException,
    PDFParsingException,
    TimeoutException,
)


def _configure_url_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> Mock:
    storage = Mock()
    storage.generate_upload_download_url.return_value = {
        "download_url": "https://storage.example/signed-source.pdf?secret=value"
    }
    storage_factory = Mock(return_value=storage)
    submit_url_task = Mock(return_value=("url-batch", "url-token"))

    monkeypatch.setattr(
        pdf_service,
        "resolve_mineru_source_s3_key",
        Mock(return_value="jobs/source.pdf"),
    )
    monkeypatch.setattr(pdf_service, "JobFileStorage", storage_factory)
    monkeypatch.setattr(pdf_service, "_submit_url_task", submit_url_task)
    return submit_url_task


def test_url_poll_parse_failure_retries_once_via_direct_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    submit_url_task = _configure_url_mode(monkeypatch)
    parse_failure = MinerUTaskFailedException(
        user_message="Failed to parse the PDF file",
        internal_message="MinerU failed with state 'failed'",
    )
    poll_mineru_task = Mock(side_effect=[parse_failure, None])
    request_upload_target = Mock(
        return_value=("direct-batch", "direct-upload-url", "direct-token")
    )
    upload_file = Mock()
    structured_logger = Mock()
    bound_logger = Mock()
    structured_logger.return_value = bound_logger

    monkeypatch.setattr(pdf_service, "poll_mineru_task", poll_mineru_task)
    monkeypatch.setattr(pdf_service, "_request_upload_target", request_upload_target)
    monkeypatch.setattr(pdf_service, "_upload_file_to_mineru", upload_file)
    monkeypatch.setattr(pdf_service, "mineru_logger", structured_logger)

    source_path = str(tmp_path / "source.pdf")
    output_dir = str(tmp_path / "output")
    pdf_service.parse_via_full(
        source_path,
        "source.pdf",
        output_dir,
        s3_key="jobs/source.pdf",
    )

    submit_url_task.assert_called_once_with(
        "https://storage.example/signed-source.pdf?secret=value",
        "source.pdf",
    )
    request_upload_target.assert_called_once_with(source_path, "source.pdf")
    upload_file.assert_called_once_with(
        source_path,
        "source.pdf",
        "direct-upload-url",
        "direct-token",
    )
    assert poll_mineru_task.call_args_list == [
        call(
            status_url=(
                f"{pdf_service.settings.MINERU_URL}"
                "/extract-results/batch/url-batch"
            ),
            task_id="url-batch",
            output_dir=output_dir,
            get_status=pdf_service.get_batch_status,
            preferred_token_id="url-token",
        ),
        call(
            status_url=(
                f"{pdf_service.settings.MINERU_URL}"
                "/extract-results/batch/direct-batch"
            ),
            task_id="direct-batch",
            output_dir=output_dir,
            get_status=pdf_service.get_batch_status,
            preferred_token_id="direct-token",
        ),
    ]

    fallback_calls = [
        logger_call
        for logger_call in structured_logger.call_args_list
        if logger_call.args
        and logger_call.args[0] == "url_mode_polling_fallback"
    ]
    assert len(fallback_calls) == 1
    assert fallback_calls[0].kwargs["failed_batch_id"] == "url-batch"
    assert "signed-source" not in repr(fallback_calls[0])
    bound_logger.warning.assert_any_call(
        "MinerU URL-mode polling reported a parse failure. "
        "Falling back to direct upload."
    )


def test_direct_upload_first_poll_parse_failure_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(pdf_service.settings, "MINERU_UPLOAD_MODE_ENABLED", True)
    request_upload_target = Mock(
        return_value=("direct-batch", "direct-upload-url", "direct-token")
    )
    upload_file = Mock()
    parse_failure = MinerUTaskFailedException(
        user_message="Failed to parse the PDF file",
        internal_message="MinerU failed with state 'failed'",
    )
    poll_mineru_task = Mock(side_effect=parse_failure)
    submit_url_task = Mock()

    monkeypatch.setattr(pdf_service, "_request_upload_target", request_upload_target)
    monkeypatch.setattr(pdf_service, "_upload_file_to_mineru", upload_file)
    monkeypatch.setattr(pdf_service, "poll_mineru_task", poll_mineru_task)
    monkeypatch.setattr(pdf_service, "_submit_url_task", submit_url_task)

    source_path = str(tmp_path / "source.pdf")
    with pytest.raises(MinerUTaskFailedException) as exc_info:
        pdf_service.parse_via_full(
            source_path,
            "source.pdf",
            str(tmp_path / "output"),
            s3_key="jobs/source.pdf",
        )

    assert exc_info.value is parse_failure
    request_upload_target.assert_called_once_with(source_path, "source.pdf")
    upload_file.assert_called_once()
    poll_mineru_task.assert_called_once()
    submit_url_task.assert_not_called()


def test_direct_upload_retry_failure_propagates_without_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_url_mode(monkeypatch)
    url_failure = MinerUTaskFailedException(
        user_message="Failed to parse the PDF file",
        internal_message="URL-mode MinerU task failed",
    )
    direct_failure = MinerUTaskFailedException(
        user_message="Failed to parse the PDF file",
        internal_message="Direct-upload MinerU task failed",
    )
    poll_mineru_task = Mock(side_effect=[url_failure, direct_failure])
    request_upload_target = Mock(
        return_value=("direct-batch", "direct-upload-url", "direct-token")
    )

    monkeypatch.setattr(pdf_service, "poll_mineru_task", poll_mineru_task)
    monkeypatch.setattr(pdf_service, "_request_upload_target", request_upload_target)
    monkeypatch.setattr(pdf_service, "_upload_file_to_mineru", Mock())

    with pytest.raises(MinerUTaskFailedException) as exc_info:
        pdf_service.parse_via_full(
            str(tmp_path / "source.pdf"),
            "source.pdf",
            str(tmp_path / "output"),
            s3_key="jobs/source.pdf",
        )

    assert exc_info.value is direct_failure
    request_upload_target.assert_called_once()
    assert poll_mineru_task.call_count == 2


def test_url_poll_success_does_not_direct_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_url_mode(monkeypatch)
    poll_mineru_task = Mock()
    request_upload_target = Mock()
    upload_file = Mock()

    monkeypatch.setattr(pdf_service, "poll_mineru_task", poll_mineru_task)
    monkeypatch.setattr(pdf_service, "_request_upload_target", request_upload_target)
    monkeypatch.setattr(pdf_service, "_upload_file_to_mineru", upload_file)

    pdf_service.parse_via_full(
        str(tmp_path / "source.pdf"),
        "source.pdf",
        str(tmp_path / "output"),
        s3_key="jobs/source.pdf",
    )

    poll_mineru_task.assert_called_once()
    request_upload_target.assert_not_called()
    upload_file.assert_not_called()


def test_url_poll_timeout_does_not_direct_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_url_mode(monkeypatch)
    timeout = TimeoutException(
        internal_message="MinerU polling timed out",
        retry_after=60,
    )
    poll_mineru_task = Mock(side_effect=timeout)
    request_upload_target = Mock()
    upload_file = Mock()

    monkeypatch.setattr(pdf_service, "poll_mineru_task", poll_mineru_task)
    monkeypatch.setattr(pdf_service, "_request_upload_target", request_upload_target)
    monkeypatch.setattr(pdf_service, "_upload_file_to_mineru", upload_file)

    with pytest.raises(TimeoutException) as exc_info:
        pdf_service.parse_via_full(
            str(tmp_path / "source.pdf"),
            "source.pdf",
            str(tmp_path / "output"),
            s3_key="jobs/source.pdf",
        )

    assert exc_info.value is timeout
    poll_mineru_task.assert_called_once()
    request_upload_target.assert_not_called()
    upload_file.assert_not_called()


def test_url_poll_other_pdf_error_does_not_direct_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_url_mode(monkeypatch)
    extraction_failure = PDFParsingException(
        user_message="Failed to extract MinerU output",
        internal_message="Invalid ZIP archive",
    )
    poll_mineru_task = Mock(side_effect=extraction_failure)
    request_upload_target = Mock()
    upload_file = Mock()

    monkeypatch.setattr(pdf_service, "poll_mineru_task", poll_mineru_task)
    monkeypatch.setattr(pdf_service, "_request_upload_target", request_upload_target)
    monkeypatch.setattr(pdf_service, "_upload_file_to_mineru", upload_file)

    with pytest.raises(PDFParsingException) as exc_info:
        pdf_service.parse_via_full(
            str(tmp_path / "source.pdf"),
            "source.pdf",
            str(tmp_path / "output"),
            s3_key="jobs/source.pdf",
        )

    assert exc_info.value is extraction_failure
    poll_mineru_task.assert_called_once()
    request_upload_target.assert_not_called()
    upload_file.assert_not_called()
