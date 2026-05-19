from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import requests
from pytest import MonkeyPatch

MINERU_RESULT_ZIP_URL = "https://mineru.example.test/results/contract.zip"


class ParserContractResult(Protocol):
    output_dir: str


class FakeMinerUJsonResponse(requests.Response):
    def __init__(self, *, status_code: int, payload: dict[str, object]) -> None:
        super().__init__()
        self.status_code = status_code
        self._content = json.dumps(payload).encode("utf-8")
        self.headers["Content-Type"] = "application/json"


class FakeMinerUZipResponse(requests.Response):
    def __init__(self, *, content: bytes) -> None:
        super().__init__()
        self.status_code = 200
        self._content = content

    def iter_content(
        self,
        chunk_size: int = 1,
        decode_unicode: bool = False,
    ) -> Iterator[bytes]:
        del decode_unicode
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def __enter__(self) -> "FakeMinerUZipResponse":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool:
        del exc_type, exc_value, traceback
        self.close()
        return False


class FakeMinerUSession(requests.Session):
    def __init__(self) -> None:
        super().__init__()
        self.submitted_url_payloads: list[dict[str, object]] = []
        self.requested_upload_payloads: list[dict[str, object]] = []
        self.uploaded_urls: list[str] = []
        self.polled_urls: list[str] = []

    def post(
        self,
        url: str,
        data: object | None = None,
        json: object | None = None,
        **kwargs: object,
    ) -> requests.Response:
        del data, kwargs
        assert isinstance(json, dict)
        if url.endswith("/extract/task/batch"):
            self.submitted_url_payloads.append(json)
            return FakeMinerUJsonResponse(
                status_code=200,
                payload={"code": 0, "data": {"batch_id": "batch-url-mode"}},
            )

        if url.endswith("/file-urls/batch"):
            self.requested_upload_payloads.append(json)
            return FakeMinerUJsonResponse(
                status_code=200,
                payload={
                    "code": 0,
                    "data": {
                        "batch_id": "batch-direct-upload",
                        "file_urls": ["https://mineru-upload.example.test/file.pdf"],
                    },
                },
            )

        raise AssertionError(f"Unexpected MinerU POST URL: {url}")

    def put(
        self,
        url: str,
        data: object | None = None,
        **kwargs: object,
    ) -> requests.Response:
        del data, kwargs
        self.uploaded_urls.append(url)
        return FakeMinerUJsonResponse(status_code=200, payload={})

    def get(self, url: str, **kwargs: object) -> requests.Response:
        del kwargs
        self.polled_urls.append(url)
        if "/extract-results/batch/" not in url:
            raise AssertionError(f"Unexpected MinerU GET URL: {url}")

        return FakeMinerUJsonResponse(
            status_code=200,
            payload={
                "code": 0,
                "data": {
                    "extract_result": {
                        "state": "done",
                        "full_zip_url": MINERU_RESULT_ZIP_URL,
                    }
                },
            },
        )


def _write_contract_pdf(pdf_path: Path) -> None:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "MinerU upload mode contract")
    document.save(pdf_path)
    document.close()


def _build_mineru_result_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zip_file:
        zip_file.writestr(
            "full.md",
            "# Contract\nParsed by the MinerU contract boundary.\n",
        )
        zip_file.writestr("layout.json", json.dumps({"pdf_info": []}))
    return buffer.getvalue()


def _patch_mineru_http_boundary(
    monkeypatch: MonkeyPatch,
    fake_session: FakeMinerUSession,
) -> None:
    result_zip = _build_mineru_result_zip()

    def build_fake_session() -> FakeMinerUSession:
        return fake_session

    def download_result_zip(url: str, **kwargs: object) -> FakeMinerUZipResponse:
        del kwargs
        assert url == MINERU_RESULT_ZIP_URL
        return FakeMinerUZipResponse(content=result_zip)

    monkeypatch.setattr(requests, "Session", build_fake_session)
    monkeypatch.setattr(requests, "get", download_result_zip)


def _patch_job_file_storage(
    monkeypatch: MonkeyPatch,
    *,
    source_exists: bool,
    storage_calls: list[tuple[str, str]],
) -> None:
    from shared.services.storage.job_file_storage import JobFileStorage

    def verify_upload_exists(
        self: JobFileStorage,
        storage_key: str,
    ) -> dict[str, object]:
        del self
        storage_calls.append(("verify", storage_key))
        return {"exists": source_exists}

    def generate_upload_download_url(
        self: JobFileStorage,
        storage_key: str,
        *,
        expires_in: int,
    ) -> dict[str, object]:
        del self
        storage_calls.append(("presign", f"{storage_key}:{expires_in}"))
        return {"download_url": f"https://files.example.test/{storage_key}"}

    def fail_source_upload(
        self: JobFileStorage,
        local_file_path: str,
        storage_key: str,
    ) -> dict[str, object]:
        del self, local_file_path
        raise AssertionError(f"Unexpected source upload for {storage_key}")

    monkeypatch.setattr(JobFileStorage, "verify_upload_exists", verify_upload_exists)
    monkeypatch.setattr(
        JobFileStorage,
        "generate_upload_download_url",
        generate_upload_download_url,
    )
    monkeypatch.setattr(JobFileStorage, "upload_source_file", fail_source_upload)


def _run_parser_contract(tmp_path: Path) -> ParserContractResult:
    from app.services.document_parser.parse_service import checkerboard_parse_output

    pdf_path = tmp_path / "contract.pdf"
    _write_contract_pdf(pdf_path)

    return checkerboard_parse_output(
        file_full_path=str(pdf_path),
        filename="contract.pdf",
        output_dir=str(tmp_path / "parser-output"),
        internal_output_filename="contract.pdf",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
        s3_key="uploads/contract.pdf",
    )


def test_pdf_parser_contract_prefers_mineru_s3_url_mode_by_default_even_in_development(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("LLM_MOCK_ENABLED", "true")
    monkeypatch.setenv("MINERU_API_KEYS", "contract-token=contract-key")
    monkeypatch.delenv("MINERU_UPLOAD_MODE_ENABLED", raising=False)
    fake_session = FakeMinerUSession()
    storage_calls: list[tuple[str, str]] = []
    _patch_mineru_http_boundary(monkeypatch, fake_session)
    _patch_job_file_storage(
        monkeypatch,
        source_exists=True,
        storage_calls=storage_calls,
    )

    parse_output = _run_parser_contract(tmp_path)

    assert Path(parse_output.output_dir, "full.md").exists()
    assert storage_calls == [
        ("verify", "uploads/contract.pdf"),
        ("presign", "uploads/contract.pdf:3600"),
    ]
    assert fake_session.submitted_url_payloads == [
        {
            "files": [{"url": "https://files.example.test/uploads/contract.pdf"}],
            "is_ocr": True,
            "enable_formula": True,
            "enable_table": True,
            "language": "auto",
            "model_version": "vlm",
        }
    ]
    assert fake_session.requested_upload_payloads == []
    assert fake_session.uploaded_urls == []
    assert fake_session.polled_urls == [
        "https://mineru.net/api/v4/extract-results/batch/batch-url-mode"
    ]


def test_pdf_parser_contract_uses_direct_upload_when_mineru_upload_mode_is_enabled(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    monkeypatch.setenv("LLM_MOCK_ENABLED", "true")
    monkeypatch.setenv("MINERU_API_KEYS", "contract-token=contract-key")
    monkeypatch.setenv("MINERU_UPLOAD_MODE_ENABLED", "true")
    fake_session = FakeMinerUSession()
    storage_calls: list[tuple[str, str]] = []
    _patch_mineru_http_boundary(monkeypatch, fake_session)
    _patch_job_file_storage(
        monkeypatch,
        source_exists=True,
        storage_calls=storage_calls,
    )

    parse_output = _run_parser_contract(tmp_path)

    assert Path(parse_output.output_dir, "full.md").exists()
    assert storage_calls == []
    assert fake_session.submitted_url_payloads == []
    assert fake_session.requested_upload_payloads == [
        {
            "files": [
                {
                    "name": "contract.pdf",
                    "is_ocr": True,
                }
            ],
            "enable_formula": True,
            "enable_table": True,
            "language": "auto",
            "model_version": "vlm",
        }
    ]
    assert fake_session.uploaded_urls == ["https://mineru-upload.example.test/file.pdf"]
    assert fake_session.polled_urls == [
        "https://mineru.net/api/v4/extract-results/batch/batch-direct-upload"
    ]
