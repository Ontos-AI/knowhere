from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from pytest import MonkeyPatch


def _load_mineru_pdf_service() -> ModuleType:
    for module_name in list(sys.modules):
        if module_name == "shared.core.config" or module_name.startswith(
            "shared.core.config."
        ):
            sys.modules.pop(module_name, None)
            continue
        if module_name == "app.services.document_parser.providers.mineru" or (
            module_name.startswith("app.services.document_parser.providers.mineru.")
        ):
            sys.modules.pop(module_name, None)

    return importlib.import_module(
        "app.services.document_parser.providers.mineru.pdf_service"
    )


def _write_pdf(tmp_path: Path) -> Path:
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    return pdf_path


def test_should_prefer_mineru_s3_url_mode_by_default_even_in_development(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("MINERU_UPLOAD_MODE_ENABLED", raising=False)
    pdf_service = _load_mineru_pdf_service()

    storage_calls: list[tuple[str, str]] = []
    submit_calls: list[tuple[str, str]] = []
    poll_calls: list[dict[str, Any]] = []

    class FakeJobFileStorage:
        def verify_upload_exists(self, storage_key: str) -> dict[str, object]:
            storage_calls.append(("verify", storage_key))
            return {"exists": True}

        def generate_upload_download_url(
            self,
            storage_key: str,
            *,
            expires_in: int,
        ) -> dict[str, str]:
            storage_calls.append(("presign", f"{storage_key}:{expires_in}"))
            return {"download_url": f"https://files.example.com/{storage_key}"}

    def fake_submit_url_task(presigned_url: str, filename: str) -> tuple[str, str]:
        submit_calls.append((presigned_url, filename))
        return "batch_url_mode", "token_url_mode"

    def fail_direct_upload(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("direct MinerU upload should not be used")

    def fake_poll_mineru_task(**kwargs: Any) -> None:
        poll_calls.append(kwargs)

    monkeypatch.setattr(pdf_service, "JobFileStorage", FakeJobFileStorage)
    monkeypatch.setattr(pdf_service, "_submit_url_task", fake_submit_url_task)
    monkeypatch.setattr(pdf_service, "_request_upload_target", fail_direct_upload)
    monkeypatch.setattr(pdf_service, "_upload_file_to_mineru", fail_direct_upload)
    monkeypatch.setattr(pdf_service, "poll_mineru_task", fake_poll_mineru_task)

    pdf_service.parse_via_full(
        str(_write_pdf(tmp_path)),
        "contract.pdf",
        str(tmp_path / "output"),
        s3_key="uploads/contract.pdf",
    )

    assert storage_calls == [
        ("verify", "uploads/contract.pdf"),
        ("presign", "uploads/contract.pdf:3600"),
    ]
    assert submit_calls == [
        ("https://files.example.com/uploads/contract.pdf", "contract.pdf")
    ]
    assert poll_calls[0]["task_id"] == "batch_url_mode"
    assert poll_calls[0]["preferred_token_id"] == "token_url_mode"


def test_should_use_direct_upload_when_mineru_upload_mode_is_enabled(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("MINERU_UPLOAD_MODE_ENABLED", "true")
    pdf_service = _load_mineru_pdf_service()

    direct_calls: list[tuple[str, str, str, str]] = []
    poll_calls: list[dict[str, Any]] = []

    class FakeJobFileStorage:
        def verify_upload_exists(self, storage_key: str) -> dict[str, object]:
            raise AssertionError(f"S3 URL mode should not inspect {storage_key}")

    def fake_request_upload_target(
        pdf_url: str,
        filename: str,
    ) -> tuple[str, str, str]:
        assert pdf_url.endswith("source.pdf")
        assert filename == "contract.pdf"
        return "batch_direct_upload", "https://mineru-upload.example.com/file", "token_direct"

    def fake_upload_file_to_mineru(
        pdf_url: str,
        filename: str,
        upload_url: str,
        token_id: str,
    ) -> None:
        direct_calls.append((pdf_url, filename, upload_url, token_id))

    def fake_poll_mineru_task(**kwargs: Any) -> None:
        poll_calls.append(kwargs)

    def fail_url_task(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("S3 URL task should not be submitted")

    monkeypatch.setattr(pdf_service, "JobFileStorage", FakeJobFileStorage)
    monkeypatch.setattr(pdf_service, "_request_upload_target", fake_request_upload_target)
    monkeypatch.setattr(pdf_service, "_upload_file_to_mineru", fake_upload_file_to_mineru)
    monkeypatch.setattr(pdf_service, "_submit_url_task", fail_url_task)
    monkeypatch.setattr(pdf_service, "poll_mineru_task", fake_poll_mineru_task)

    pdf_path = _write_pdf(tmp_path)
    pdf_service.parse_via_full(
        str(pdf_path),
        "contract.pdf",
        str(tmp_path / "output"),
        s3_key="uploads/contract.pdf",
    )

    assert direct_calls == [
        (
            str(pdf_path),
            "contract.pdf",
            "https://mineru-upload.example.com/file",
            "token_direct",
        )
    ]
    assert poll_calls[0]["task_id"] == "batch_direct_upload"
    assert poll_calls[0]["preferred_token_id"] == "token_direct"


def test_should_use_direct_upload_when_mineru_upload_mode_is_enabled_from_dotenv(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("MINERU_UPLOAD_MODE_ENABLED", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MINERU_UPLOAD_MODE_ENABLED=true\n")
    pdf_service = _load_mineru_pdf_service()

    direct_calls: list[str] = []

    class FakeJobFileStorage:
        def verify_upload_exists(self, storage_key: str) -> dict[str, object]:
            raise AssertionError(f"S3 URL mode should not inspect {storage_key}")

    def fake_request_upload_target(
        pdf_url: str,
        filename: str,
    ) -> tuple[str, str, str]:
        assert pdf_url.endswith("source.pdf")
        assert filename == "contract.pdf"
        return "batch_dotenv_direct", "https://mineru-upload.example.com/file", "token"

    def fake_upload_file_to_mineru(
        pdf_url: str,
        filename: str,
        upload_url: str,
        token_id: str,
    ) -> None:
        del pdf_url, filename, upload_url, token_id
        direct_calls.append("uploaded")

    def fake_poll_mineru_task(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(pdf_service, "JobFileStorage", FakeJobFileStorage)
    monkeypatch.setattr(pdf_service, "_request_upload_target", fake_request_upload_target)
    monkeypatch.setattr(pdf_service, "_upload_file_to_mineru", fake_upload_file_to_mineru)
    monkeypatch.setattr(pdf_service, "poll_mineru_task", fake_poll_mineru_task)

    pdf_service.parse_via_full(
        str(_write_pdf(tmp_path)),
        "contract.pdf",
        str(tmp_path / "output"),
        s3_key="uploads/contract.pdf",
    )

    assert direct_calls == ["uploaded"]
