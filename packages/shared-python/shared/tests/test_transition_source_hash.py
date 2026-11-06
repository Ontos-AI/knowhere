"""Reviewed transition authorization binds dependent runtime and contract source."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-source-hash-unit")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest
from shared.services.retrieval.lexical.transition import LexicalTransition


@pytest.mark.parametrize(
    "relative",
    [
        "packages/shared-python/shared/services/retrieval/scoring/score_units.py",
        "packages/shared-python/shared/utils/text_utils.py",
        "packages/shared-python/shared/core/config/publication.py",
        "apps/api/tests/contract/test_opensearch_ranking_replay.py",
        "scripts/probes/sdk-http-node.mjs",
    ],
)
def test_source_hash_changes_when_dependent_runtime_or_qualification_changes(
    relative: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline: str = LexicalTransition.getSourceHash()
    target: Path = Path(__file__).resolve().parents[4] / relative
    assert target.exists()
    readBytes = Path.read_bytes

    def readChanged(path: Path) -> bytes:
        original: bytes = readBytes(path)
        return (
            original + b"\n# simulated qualification/runtime change\n"
            if path == target
            else original
        )

    monkeypatch.setattr(Path, "read_bytes", readChanged)
    assert LexicalTransition.getSourceHash() != baseline


@pytest.mark.parametrize(
    "relative",
    [
        "apps/api/alembic/sql/schema.sql",
        "deploy/opensearch/templates/native.service",
        "deploy/opensearch/templates/backup.timer",
        "deploy/opensearch/runtime.env.example",
        "deploy/opensearch/templates/configuration.template",
        "deploy/opensearch/templates/configuration.j2",
        "deploy/opensearch/Dockerfile",
        "deploy/docker/Dockerfile.api",
        "deploy/docker/Dockerfile.worker",
        "deploy/docker/.dockerignore",
        "apps/api/main.py",
        "apps/worker/worker.py",
    ],
)
def test_source_hash_binds_migrations_runtime_and_operator_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    from shared.services.retrieval.lexical import transition

    repository: Path = tmp_path / "repository"
    module: Path = (
        repository
        / "packages/shared-python/shared/services/retrieval/lexical/transition.py"
    )
    module.parent.mkdir(parents=True)
    module.write_text("# reviewed source\n")
    resource: Path = repository / relative
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text("reviewed dependency\n")
    monkeypatch.setattr(transition, "__file__", str(module))
    before: str = LexicalTransition.getSourceHash()
    resource.write_text("changed dependency\n")
    assert LexicalTransition.getSourceHash() != before


@pytest.mark.parametrize(
    "relative",
    [
        "deploy/opensearch/secrets/credentials.json",
        "deploy/opensearch/runtime/node-state.json",
        "deploy/opensearch/receipts/backup.json",
        "deploy/opensearch/build/rendered.json",
        "deploy/opensearch/dist/artifact.json",
    ],
)
def test_source_hash_excludes_private_material_and_generated_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    from shared.services.retrieval.lexical import transition

    repository: Path = tmp_path / "repository"
    module: Path = (
        repository
        / "packages/shared-python/shared/services/retrieval/lexical/transition.py"
    )
    module.parent.mkdir(parents=True)
    module.write_text("# reviewed source\n")
    material: Path = repository / relative
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_text("synthetic private or generated data\n")
    monkeypatch.setattr(transition, "__file__", str(module))
    before: str = LexicalTransition.getSourceHash()
    material.write_text("rotated synthetic private or generated data\n")
    assert LexicalTransition.getSourceHash() == before
