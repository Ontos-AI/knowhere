"""Bind OpenSearch operator and workload observation to reviewed source."""

from hashlib import sha256
from pathlib import Path

from shared.services.retrieval.lexical.preparation import LexicalPreparation


def getSourceHash() -> str:
    repository: Path = Path(__file__).resolve().parents[6]
    digest = sha256()
    # Bind dependent token builders, publication configuration, migrations,
    # runtime and checked-in qualification fixtures to the reviewed source.
    # External frozen corpora/inventories have their own receipt hashes.
    roots: tuple[str, ...] = (
        "packages/shared-python/shared",
        "apps/api/app",
        "apps/worker/app",
        "apps/api/alembic",
        "apps/api/tests",
        "apps/worker/tests",
        "scripts/probes",
        "deploy",
    )
    suffixes: frozenset[str] = frozenset(
        {
            ".py",
            ".mjs",
            ".json",
            ".txt",
            ".toml",
            ".yaml",
            ".yml",
            ".sh",
            ".sql",
            ".service",
            ".timer",
            ".example",
            ".template",
            ".j2",
            ".jinja",
            ".jinja2",
            ".conf",
            ".ini",
            ".cfg",
            ".properties",
            ".mako",
        }
    )
    names: frozenset[str] = frozenset(
        {"Dockerfile", "Containerfile", "Makefile", "Procfile", ".dockerignore"}
    )
    excluded: frozenset[str] = frozenset(
        {
            "__pycache__",
            ".git",
            ".venv",
            "node_modules",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "build",
            "dist",
            "logs",
        }
    )
    operatorOutputs: frozenset[str] = frozenset(
        {
            "secrets",
            "credentials",
            "runtime",
            "state",
            "receipts",
            "artifacts",
            "output",
            "outputs",
        }
    )

    def shouldInclude(source: Path) -> bool:
        parts: tuple[str, ...] = source.relative_to(repository).parts
        return (
            source.is_file()
            and (source.suffix in suffixes or source.name in names
                 or source.name.startswith(("Dockerfile.", "Containerfile.")))
            and not excluded.intersection(parts[:-1])
            and not (
                parts[0] == "deploy" and operatorOutputs.intersection(parts[1:-1])
            )
        )

    sources: set[Path] = {
        source
        for root in roots
        for source in (repository / root).rglob("*")
        if shouldInclude(source)
    }
    for application in ("apps/api", "apps/worker"):
        sources.update(
            source
            for source in (repository / application).glob("*")
            if shouldInclude(source)
        )
    sources.update(
        repository / name
        for name in (
            "pyproject.toml",
            "uv.lock",
            ".python-version",
            "Makefile",
            "packages/shared-python/pyproject.toml",
            "apps/api/pyproject.toml",
            "apps/worker/pyproject.toml",
        )
        if (repository / name).exists()
    )
    for source in sorted(sources):
        data: bytes = source.read_bytes()
        digest.update(str(source.relative_to(repository)).encode() + b"\0")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    digest.update(LexicalPreparation.getCompilerFingerprint().encode())
    return digest.hexdigest()
