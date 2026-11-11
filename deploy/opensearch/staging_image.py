"""Render a source-bound staging image context without building or deploying it."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
from tempfile import TemporaryDirectory
from typing import Literal, cast

__all__: list[str] = ["StagingImageContext"]

_DIGEST: re.Pattern[str] = re.compile(r"[a-f0-9]{64}")
_IMAGE: re.Pattern[str] = re.compile(r"[a-z0-9][a-z0-9./_:-]*@sha256:[a-f0-9]{64}")
_REQUIRED: frozenset[str] = frozenset({
    "uv.lock", "pyproject.toml", "apps/api/main.py", "apps/api/alembic/env.py", "apps/worker/worker.py",
    "deploy/docker/Dockerfile.api", "deploy/docker/Dockerfile.worker",
    "deploy/ecs/task-definition-api.staging.json", "deploy/ecs/task-definition-worker.staging.json",
    "scripts/probes/prepared-completion-task.py", "scripts/probes/opensearch-workload.py",
    "packages/shared-python/shared/services/retrieval/lexical/sourcehash.py",
    "packages/shared-python/shared/services/retrieval/lexical/stage_observer.py",
    "packages/shared-python/shared/services/retrieval/lexical/transition.py",
})
_EXCLUDED: frozenset[str] = frozenset({".git", ".venv", "node_modules", "__pycache__", "secrets", "credentials"})
_SOURCE_ROOT: str = "/reviewed/source"


def _readManifest(data: bytes) -> dict[str, tuple[str, int]]:
    value: object = json.loads(data)
    if not isinstance(value, dict) or not isinstance(value.get("files"), list) or not 1 <= len(value["files"]) <= 10_000:
        raise ValueError("Source manifest must contain a bounded file inventory")
    files: dict[str, tuple[str, int]] = {}
    for item in value["files"]:
        if not isinstance(item, dict):
            raise ValueError("Source manifest entry must be an object")
        name: object = item.get("path")
        digest: object = item.get("sha256")
        size: object = item.get("bytes")
        if not isinstance(name, str) or not name or "\\" in name or any(ord(character) < 32 for character in name):
            raise ValueError("Source manifest contains an invalid path")
        relative: PurePosixPath = PurePosixPath(name)
        if (relative.is_absolute() or str(relative) != name or ".." in relative.parts
                or _EXCLUDED.intersection(relative.parts) or ".private." in name
                or relative.suffix in (".key", ".pem", ".p12", ".pfx")
                or (relative.name.startswith(".env") and not relative.name.endswith(".example"))):
            raise ValueError("Source manifest contains an unsafe or private path")
        if name in files or not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None or type(size) is not int or size < 0:
            raise ValueError("Source manifest contains a duplicate or invalid file identity")
        files[name] = (digest, size)
    if not _REQUIRED <= files.keys() or sum(size for _, size in files.values()) > 512 * 1024 * 1024:
        raise ValueError("Source manifest lacks the reviewed runtime closure or exceeds its expanded byte bound")
    return files


def _extractReviewedSource(archive: Path, output: Path, files: dict[str, tuple[str, int]]) -> None:
    seen: set[str] = set()
    with tarfile.open(archive, "r|gz") as stream:
        for member in stream:
            expected: tuple[str, int] | None = files.get(member.name)
            if not member.isfile() or member.name in seen or expected is None or member.size != expected[1]:
                raise ValueError("Archive contains a non-file, duplicate or entry outside the source manifest")
            payload = stream.extractfile(member)
            if payload is None:
                raise ValueError("Archive cannot supply a reviewed source file")
            destination: Path = output / member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = sha256()
            with payload, destination.open("xb") as target:
                while block := payload.read(1024 * 1024):
                    digest.update(block)
                    target.write(block)
            if digest.hexdigest() != expected[0]:
                raise ValueError("Archive file bytes differ from the reviewed manifest")
            destination.chmod(0o755 if member.mode & 0o111 else 0o644)
            seen.add(member.name)
    if seen != files.keys():
        raise ValueError("Archive omits files in the reviewed source manifest")


def _getBuildPlatform(source: Path, service: Literal["api", "worker"]) -> str:
    definition: object = json.loads((source / f"deploy/ecs/task-definition-{service}.staging.json").read_bytes())
    runtime: object = definition.get("runtimePlatform") if isinstance(definition, dict) else None
    if isinstance(runtime, dict) and runtime.get("operatingSystemFamily") == "LINUX":
        architecture: object = runtime.get("cpuArchitecture")
        if architecture == "X86_64":
            return "linux/amd64"
        if architecture == "ARM64":
            return "linux/arm64"
    raise ValueError("Reviewed staging task definition has an unsupported runtime platform")


class StagingImageContext:
    @staticmethod
    def render(archive: Path, manifest: Path, output: Path, *, service: Literal["api", "worker"],
               baseImage: str, archiveSha256: str, manifestSha256: str) -> dict[str, object]:
        if service not in ("api", "worker") or _IMAGE.fullmatch(baseImage) is None:
            raise ValueError("Use an API or worker base image pinned by a SHA-256 digest")
        if output.exists() or output.is_symlink() or not output.parent.is_dir():
            raise ValueError("Use a new staging context in an existing private directory")
        if archive.stat().st_size > 64 * 1024 * 1024 or manifest.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Reviewed source archive or manifest exceeds its byte bound")
        manifestData: bytes = manifest.read_bytes()
        if sha256(archive.read_bytes()).hexdigest() != archiveSha256 or sha256(manifestData).hexdigest() != manifestSha256:
            raise ValueError("Source archive or manifest checksum does not match the reviewed artifacts")
        files: dict[str, tuple[str, int]] = _readManifest(manifestData)
        workDirectory: str = f"{_SOURCE_ROOT}/apps/{service}"
        searchPaths: str = f"{workDirectory}:{_SOURCE_ROOT}/packages/shared-python:{_SOURCE_ROOT}"
        tag: str = f"knowhere-opensearch-stage-{service}:{archiveSha256[:16]}"
        receipt: dict[str, object] = {
            "schema_version": "opensearch-staging-image-context-v1", "service": service,
            "base_image": baseImage, "source_archive_sha256": archiveSha256, "source_manifest_sha256": manifestSha256,
            "reviewed_files": len(files), "built": False, "published": False, "deployed": False,
            "runtime_source_hash_command": ["/app/venv/bin/python", "-c",
                "from shared.services.retrieval.lexical.transition import LexicalTransition; print(LexicalTransition.getSourceHash())"],
        }
        with TemporaryDirectory(prefix=".opensearch-stage-", dir=output.parent) as temporary:
            context: Path = Path(temporary) / "context"
            context.mkdir(mode=0o700)
            source: Path = context / "source"
            _extractReviewedSource(archive, source, files)
            platform: str = _getBuildPlatform(source, service)
            receipt["platform"] = platform
            receipt["build_command"] = ["docker", "build", "--pull=false", "--network=none",
                f"--platform={platform}", f"--tag={tag}", str(output.resolve())]
            (context / "source-manifest.json").write_bytes(manifestData)
            demoAssets: str = ""
            if service == "api":
                # Binary demo assets are supplied by the pinned base. The catalog
                # resolves them beside the reviewed source and disallows external links.
                demoAssets = (
                    "RUN cd /app/app/data/demo_documents && find . -type f -name '*.png' "
                    "-exec sh -ec 'destination=$1; shift; for asset do "
                    'demo=${asset#./}; demo=${demo%%/*}; '
                    'if [ -d "$destination/$demo" ] && [ ! -e "$destination/$asset" ]; then '
                    'mkdir -p "$destination/${asset%/*}" && cp "$asset" "$destination/$asset" || exit 1; '
                    "fi; done' sh /reviewed/source/apps/api/app/data/demo_documents {} +\n"
                )
            (context / "Dockerfile").write_text(
                f"FROM {baseImage}\n"
                f"COPY --chown=appuser:appuser source/ {_SOURCE_ROOT}/\n"
                f"{demoAssets}"
                "COPY --chown=appuser:appuser source-manifest.json /reviewed/source-manifest.json\n"
                f"WORKDIR {workDirectory}\n"
                f"ENV PYTHONPATH={searchPaths}\n"
                "ENV PYTHONDONTWRITEBYTECODE=1\n"
                f"LABEL io.knowhere.source.archive-sha256={archiveSha256} "
                f"io.knowhere.source.manifest-sha256={manifestSha256}\n"
            )
            (context / "context-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            if output.exists() or output.is_symlink():
                raise ValueError("Staging context destination became occupied")
            context.rename(output)
        return receipt


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--service", choices=("api", "worker"), required=True)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    arguments: argparse.Namespace = parser.parse_args()
    receipt: dict[str, object] = StagingImageContext.render(arguments.archive, arguments.manifest, arguments.output,
        service=cast(Literal["api", "worker"], arguments.service), baseImage=arguments.base_image,
        archiveSha256=arguments.archive_sha256, manifestSha256=arguments.manifest_sha256)
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (ValueError, OSError, tarfile.TarError) as error:
        print(f"Staging image context refused: {error}", file=sys.stderr)
        raise SystemExit(2) from None
