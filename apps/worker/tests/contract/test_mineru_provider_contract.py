from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_parser.providers.mineru.artifact_contract import (
    MinerUArtifactBundle,
    MinerUArtifactManifest,
)
from app.services.document_parser.providers.mineru.local_process import LocalMinerUError
from shared.core.config.mineru import MineruConfig

from app.services.document_parser.providers.mineru import local_pdf_service, provider


def _bundle(request: object) -> MinerUArtifactBundle:
    root = request.output_root
    root.mkdir(parents=True, exist_ok=True)
    markdown = root / "document.md"
    markdown.write_text("# Local result\n", encoding="utf-8")
    middle = root / "middle.json"
    middle.write_text("{}", encoding="utf-8")
    content = root / "content.json"
    content.write_text("[]", encoding="utf-8")
    content_v2 = root / "content-v2.json"
    content_v2.write_text("[]", encoding="utf-8")
    images = root / "images"
    images.mkdir()
    (images / "figure.png").write_bytes(b"png")
    manifest_path = root / "mineru_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    log_path = root.parent / "logs" / "mineru.log"
    log_path.parent.mkdir()
    log_path.write_text("safe local log", encoding="utf-8")
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
    return MinerUArtifactBundle(
        manifest_path=manifest_path,
        output_root=root,
        markdown_path=markdown,
        middle_json_path=middle,
        content_list_path=content,
        content_list_v2_path=content_v2,
        images_dir=images,
        manifest=manifest,
    )


def test_cloud_provider_is_default_and_delegates_all_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(provider.settings, "MINERU_PROVIDER", "cloud")
    monkeypatch.setattr(
        provider,
        "parse_via_full",
        lambda *args, **kwargs: calls.append((*args, kwargs)),
    )
    monkeypatch.setattr(
        provider,
        "parse_via_local",
        lambda *args, **kwargs: pytest.fail("local provider must not be called"),
    )

    provider.parse_pdf("source.pdf", "document.pdf", str(tmp_path), s3_key="in/key")

    assert calls == [
        ("source.pdf", "document.pdf", str(tmp_path), {"s3_key": "in/key"})
    ]
    assert MineruConfig().MINERU_PROVIDER == "cloud"


def test_local_provider_materializes_artifacts_without_cloud_or_raw_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF")
    project = tmp_path / "MinerU"
    project.mkdir()
    uv = tmp_path / "uv.exe"
    uv.write_bytes(b"uv")
    output = tmp_path / "output"

    class FakeRunner:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self, request: object) -> MinerUArtifactBundle:
            return _bundle(request)

    monkeypatch.setattr(local_pdf_service, "LocalMinerURunner", FakeRunner)
    monkeypatch.setattr(provider.settings, "MINERU_PROVIDER", "local")
    monkeypatch.setattr(provider.settings, "MINERU_LOCAL_PROJECT_PATH", str(project))
    monkeypatch.setattr(provider.settings, "MINERU_LOCAL_UV_EXECUTABLE", str(uv))
    monkeypatch.setattr(
        provider,
        "parse_via_full",
        lambda *args, **kwargs: pytest.fail("cloud provider must not be called"),
    )

    provider.parse_pdf(str(source), source.name, str(output), s3_key="ignored/key")

    assert (output / "full.md").read_text(encoding="utf-8") == "# Local result\n"
    assert (output / "images" / "figure.png").read_bytes() == b"png"
    assert (output / "logs" / "mineru.log").read_text(encoding="utf-8") == (
        "safe local log"
    )
    assert not any(path.name.startswith(".mineru-local-") for path in output.iterdir())
    assert MineruConfig().MINERU_LOCAL_SHARD_CONCURRENCY == 1


@pytest.mark.parametrize(
    ("source", "project", "uv", "message"),
    [
        ("https://example.test/document.pdf", "configured", "configured", "local file"),
        ("source.pdf", "", "configured", "project"),
        ("source.pdf", "configured", "", "uv"),
    ],
)
def test_local_provider_rejects_remote_or_missing_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    project: str,
    uv: str,
    message: str,
) -> None:
    local_source = tmp_path / "source.pdf"
    local_source.write_bytes(b"%PDF")
    project_path = tmp_path / "MinerU"
    project_path.mkdir()
    uv_path = tmp_path / "uv.exe"
    uv_path.write_bytes(b"uv")
    source_value = source if source.startswith("https") else str(local_source)
    project_value = str(project_path) if project else ""
    uv_value = str(uv_path) if uv else ""
    monkeypatch.setattr(provider.settings, "MINERU_PROVIDER", "local")
    monkeypatch.setattr(provider.settings, "MINERU_LOCAL_PROJECT_PATH", project_value)
    monkeypatch.setattr(provider.settings, "MINERU_LOCAL_UV_EXECUTABLE", uv_value)

    with pytest.raises(ValueError, match=message):
        provider.parse_pdf(source_value, "document.pdf", str(tmp_path / "output"))


def test_local_provider_failure_removes_partial_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF")
    project = tmp_path / "MinerU"
    project.mkdir()
    uv = tmp_path / "uv.exe"
    uv.write_bytes(b"uv")
    output = tmp_path / "output"

    class FailingRunner:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self, request: object) -> None:
            request.output_root.mkdir(parents=True)
            (request.output_root / "partial.md").write_text("partial", encoding="utf-8")
            raise LocalMinerUError(
                "local failed",
                return_code=2,
                timed_out=False,
                stderr_tail="",
                log_path=request.output_root.parent / "logs" / "mineru.log",
            )

    monkeypatch.setattr(local_pdf_service, "LocalMinerURunner", FailingRunner)
    monkeypatch.setattr(provider.settings, "MINERU_PROVIDER", "local")
    monkeypatch.setattr(provider.settings, "MINERU_LOCAL_PROJECT_PATH", str(project))
    monkeypatch.setattr(provider.settings, "MINERU_LOCAL_UV_EXECUTABLE", str(uv))

    with pytest.raises(LocalMinerUError, match="local failed"):
        provider.parse_pdf(str(source), source.name, str(output))

    assert not (output / "full.md").exists()
    assert not (output / "images").exists()
    assert not any(path.name.startswith(".mineru-local-") for path in output.iterdir())
