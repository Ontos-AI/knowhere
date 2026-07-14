from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.document_parser.providers.mineru.artifact_contract import (
    MinerUArtifactContractError,
    validate_mineru_artifact_bundle,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_valid_bundle(
    tmp_path: Path,
    *,
    images_status: str | None = None,
) -> tuple[Path, Path, Path, dict]:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.7 synthetic")
    output_root = tmp_path / "mineru-output"
    parse_dir = output_root / "report" / "auto"
    parse_dir.mkdir(parents=True)
    files = {
        "markdown": (parse_dir / "report.md", "# Synthetic\n"),
        "middle_json": (
            parse_dir / "report_middle.json",
            json.dumps({"pdf_info": [{"page_idx": 0}]}),
        ),
        "content_list": (
            parse_dir / "report_content_list.json",
            json.dumps([{"type": "text", "text": "Synthetic"}]),
        ),
        "content_list_v2": (
            parse_dir / "report_content_list_v2.json",
            json.dumps([[{"type": "paragraph", "content": []}]]),
        ),
    }
    artifacts: dict[str, dict[str, str]] = {}
    for name, (path, content) in files.items():
        path.write_text(content, encoding="utf-8")
        artifacts[name] = {
            "path": path.relative_to(output_root).as_posix(),
            "sha256": _sha256(path),
        }

    images_dir = parse_dir / "images"
    images_artifact = {"path": images_dir.relative_to(output_root).as_posix()}
    if images_status is None:
        images_dir.mkdir()
    else:
        images_artifact["status"] = images_status
    artifacts["images_dir"] = images_artifact

    manifest = {
        "schema_version": "knowhere-mineru-artifacts/1.0",
        "status": "completed",
        "source": {
            "filename": source.name,
            "suffix": ".pdf",
            "sha256": _sha256(source),
            "size_bytes": source.stat().st_size,
        },
        "parser": {
            "name": "MinerU",
            "version": "3.4.4",
            "git_commit": None,
            "backend_requested": "pipeline",
            "backend_effective": "pipeline",
            "method": "auto",
            "language": "en",
            "formula_enabled": True,
            "table_enabled": True,
            "image_analysis_enabled": False,
        },
        "execution": {
            "mode": "local-direct-python",
            "offline_requested": True,
            "offline_verified": False,
            "started_at": "2026-07-13T12:00:00Z",
            "completed_at": "2026-07-13T12:00:01Z",
        },
        "document": {"logical_page_count": 1},
        "artifacts": artifacts,
        "warnings": [],
    }
    manifest_path = output_root / "mineru_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return source, output_root, manifest_path, manifest


def test_valid_manifest_returns_typed_artifact_bundle(tmp_path: Path) -> None:
    source, output_root, manifest_path, _manifest = _write_valid_bundle(tmp_path)

    bundle = validate_mineru_artifact_bundle(
        manifest_path=manifest_path,
        output_root=output_root,
        source_path=source,
    )

    assert bundle.manifest.schema_version == "knowhere-mineru-artifacts/1.0"
    assert bundle.middle_json_path.name == "report_middle.json"
    assert bundle.content_list_v2_path.name == "report_content_list_v2.json"
    assert bundle.images_dir.is_dir()


def test_malformed_manifest_json_fails(tmp_path: Path) -> None:
    source, output_root, manifest_path, _manifest = _write_valid_bundle(tmp_path)
    manifest_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(MinerUArtifactContractError, match="valid JSON"):
        validate_mineru_artifact_bundle(manifest_path, output_root, source)


def test_unknown_schema_version_fails(tmp_path: Path) -> None:
    source, output_root, manifest_path, manifest = _write_valid_bundle(tmp_path)
    manifest["schema_version"] = "knowhere-mineru-artifacts/2.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(MinerUArtifactContractError, match="schema version"):
        validate_mineru_artifact_bundle(manifest_path, output_root, source)


def test_additive_manifest_fields_are_allowed(tmp_path: Path) -> None:
    source, output_root, manifest_path, manifest = _write_valid_bundle(tmp_path)
    manifest["future_optional_field"] = {"value": True}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    bundle = validate_mineru_artifact_bundle(manifest_path, output_root, source)

    assert bundle.manifest.raw["future_optional_field"] == {"value": True}


def test_artifact_path_traversal_fails(tmp_path: Path) -> None:
    source, output_root, manifest_path, manifest = _write_valid_bundle(tmp_path)
    manifest["artifacts"]["middle_json"]["path"] = "../outside.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(MinerUArtifactContractError, match="relative path"):
        validate_mineru_artifact_bundle(manifest_path, output_root, source)


def test_artifact_hash_mismatch_fails(tmp_path: Path) -> None:
    source, output_root, manifest_path, manifest = _write_valid_bundle(tmp_path)
    manifest["artifacts"]["markdown"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(MinerUArtifactContractError, match="hash mismatch"):
        validate_mineru_artifact_bundle(manifest_path, output_root, source)


def test_source_identity_mismatch_fails(tmp_path: Path) -> None:
    source, output_root, manifest_path, manifest = _write_valid_bundle(tmp_path)
    manifest["source"]["filename"] = "other.pdf"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(MinerUArtifactContractError, match="source filename"):
        validate_mineru_artifact_bundle(manifest_path, output_root, source)


def test_missing_images_directory_requires_documented_not_generated_status(
    tmp_path: Path,
) -> None:
    source, output_root, manifest_path, _manifest = _write_valid_bundle(
        tmp_path,
        images_status="not_generated",
    )

    bundle = validate_mineru_artifact_bundle(manifest_path, output_root, source)

    assert not bundle.images_dir.exists()


def test_undocumented_missing_images_directory_fails(tmp_path: Path) -> None:
    source, output_root, manifest_path, _manifest = _write_valid_bundle(tmp_path)
    (output_root / "report" / "auto" / "images").rmdir()

    with pytest.raises(MinerUArtifactContractError, match="images_dir"):
        validate_mineru_artifact_bundle(manifest_path, output_root, source)


def test_required_json_artifacts_are_parsed_before_success(tmp_path: Path) -> None:
    source, output_root, manifest_path, _manifest = _write_valid_bundle(tmp_path)
    (output_root / "report" / "auto" / "report_middle.json").write_text(
        "[]", encoding="utf-8"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    middle_path = output_root / manifest["artifacts"]["middle_json"]["path"]
    manifest["artifacts"]["middle_json"]["sha256"] = _sha256(middle_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(MinerUArtifactContractError, match="middle_json"):
        validate_mineru_artifact_bundle(manifest_path, output_root, source)

