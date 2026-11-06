"""A staging image context must contain only the exact reviewed source."""

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_BASE_IMAGE: str = "registry.example.invalid/knowhere-worker@sha256:" + "a" * 64
_FILES: dict[str, bytes] = {
    name: b"# reviewed synthetic source\n" for name in (
        "uv.lock", "pyproject.toml", "apps/api/main.py", "apps/api/alembic/env.py",
        "apps/worker/worker.py", "deploy/docker/Dockerfile.api", "deploy/docker/Dockerfile.worker",
        "scripts/probes/prepared-completion-task.py", "scripts/probes/opensearch-workload.py",
        "packages/shared-python/shared/services/retrieval/lexical/stage_observer.py",
        "packages/shared-python/shared/services/retrieval/lexical/transition.py",
    )
} | {
    f"deploy/ecs/task-definition-{service}.staging.json": json.dumps({
        "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
    }).encode() for service in ("api", "worker")
}


def createSource(directory: Path, files: dict[str, bytes], *, changedFile: str | None = None) -> tuple[Path, Path]:
    archive: Path = directory / "source.tar.gz"
    manifest: Path = directory / "source-manifest.json"
    manifest.write_text(json.dumps({"base": "b" * 40, "files": [
        {"path": name, "sha256": sha256(data).hexdigest(), "bytes": len(data)} for name, data in files.items()
    ]}))
    with tarfile.open(archive, "w:gz") as stream:
        for name, original in files.items():
            data: bytes = b"# edited after review\n" if name == changedFile else original
            member = tarfile.TarInfo(name)
            member.size = len(data)
            stream.addfile(member, BytesIO(data))
    return archive, manifest


def render(source: tuple[Path, Path], output: Path, *, baseImage: str = _BASE_IMAGE) -> dict[str, object]:
    from staging_image import StagingImageContext

    archive, manifest = source
    return StagingImageContext.render(archive, manifest, output, service="worker", baseImage=baseImage,
        archiveSha256=sha256(archive.read_bytes()).hexdigest(), manifestSha256=sha256(manifest.read_bytes()).hexdigest())


def test_overlay_preserves_reviewed_sources_and_existing_worker_entrypoint(tmp_path: Path) -> None:
    output: Path = tmp_path / "context"
    source: tuple[Path, Path] = createSource(tmp_path, _FILES)
    receipt: dict[str, object] = render(source, output)
    assert {str(file.relative_to(output / "source")): file.read_bytes() for file in (output / "source").rglob("*") if file.is_file()} == _FILES
    assert (output / "source-manifest.json").read_bytes() == source[1].read_bytes()
    dockerfile: str = (output / "Dockerfile").read_text()
    assert f"FROM {_BASE_IMAGE}" in dockerfile and "WORKDIR /reviewed/source/apps/worker" in dockerfile
    assert "/reviewed/source/packages/shared-python" in dockerfile
    assert "ENTRYPOINT" not in dockerfile
    assert receipt["built"] is False and receipt["published"] is False
    buildCommand: object = receipt["build_command"]
    assert isinstance(buildCommand, list) and "--network=none" in buildCommand
    assert "--platform=linux/amd64" in buildCommand
    assert receipt["platform"] == "linux/amd64"


def test_api_overlay_keeps_demo_images_from_the_pinned_base(tmp_path: Path) -> None:
    from staging_image import StagingImageContext

    demoPath: str = "apps/api/app/data/demo_documents"
    source, manifest = createSource(tmp_path, {
        **_FILES,
        f"{demoPath}/demo/chunks.json": b"[]",
        f"{demoPath}/demo/images/updated.png": b"reviewed image",
    })
    output: Path = tmp_path / "context"
    StagingImageContext.render(source, manifest, output, service="api", baseImage=_BASE_IMAGE,
        archiveSha256=sha256(source.read_bytes()).hexdigest(), manifestSha256=sha256(manifest.read_bytes()).hexdigest())
    base: Path = tmp_path / "base"
    originalPage: Path = base / "demo/page_citation_assets/page-1.png"
    originalPage.parent.mkdir(parents=True)
    originalPage.write_bytes(b"base page")
    baseImage: Path = base / "demo/images/updated.png"
    baseImage.parent.mkdir()
    baseImage.write_bytes(b"base image")
    (base / "demo/chunks.json").write_bytes(b"old chunks")
    orphanImage: Path = base / "removed-demo/images/image.png"
    orphanImage.parent.mkdir(parents=True)
    orphanImage.write_bytes(b"removed image")
    staged: Path = output / "source" / demoPath

    for line in (output / "Dockerfile").read_text().splitlines():
        if line.startswith("RUN "):
            command: str = line[4:].replace(
                f"/reviewed/source/{demoPath}", str(staged),
            ).replace("/app/app/data/demo_documents", str(base))
            subprocess.run(["sh", "-ec", command], check=True)

    assert (staged / "demo/page_citation_assets/page-1.png").read_bytes() == b"base page"
    assert (staged / "demo/images/updated.png").read_bytes() == b"reviewed image"
    assert (staged / "demo/chunks.json").read_bytes() == b"[]"
    assert not (staged / "removed-demo").exists()


@pytest.mark.parametrize("name", ["../escaped.py", "apps/worker/.env", "data/input.private.json"])
def test_invalid_reviewed_path_cannot_create_context(tmp_path: Path, name: str) -> None:
    source: tuple[Path, Path] = createSource(tmp_path, {**_FILES, name: b"synthetic private data"})
    output: Path = tmp_path / "context"
    with pytest.raises(ValueError, match="path"):
        render(source, output)
    assert not output.exists()


def test_archive_payload_cannot_differ_from_reviewed_files(tmp_path: Path) -> None:
    output: Path = tmp_path / "context"
    source: tuple[Path, Path] = createSource(tmp_path, _FILES, changedFile="apps/worker/worker.py")
    with pytest.raises(ValueError, match="manifest"):
        render(source, output)
    assert not output.exists()


def test_staging_base_image_cannot_be_a_mutable_tag(tmp_path: Path) -> None:
    output: Path = tmp_path / "context"
    source: tuple[Path, Path] = createSource(tmp_path, _FILES)
    with pytest.raises(ValueError, match="digest"):
        render(source, output, baseImage="knowhere-worker:latest")
    assert not output.exists()


@pytest.mark.parametrize(("architecture", "expected"), [("ARM64", "linux/arm64"), ("invalid", None)])
def test_platform_comes_from_reviewed_task_definition(
    tmp_path: Path, architecture: str, expected: str | None,
) -> None:
    definition: bytes = json.dumps({
        "runtimePlatform": {"cpuArchitecture": architecture, "operatingSystemFamily": "LINUX"},
    }).encode()
    source: tuple[Path, Path] = createSource(tmp_path, {
        **_FILES, "deploy/ecs/task-definition-worker.staging.json": definition,
    })
    output: Path = tmp_path / "context"
    if expected is None:
        with pytest.raises(ValueError, match="platform"):
            render(source, output)
        assert not output.exists()
        return
    receipt: dict[str, object] = render(source, output)
    assert receipt["platform"] == expected
