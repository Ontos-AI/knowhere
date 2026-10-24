from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType
from typing import Any

from pytest import MonkeyPatch

from tests.support.import_environment import (
    configure_import_environment,
    ensure_import_paths,
)


DEMO_SOURCE_ID = "demo-spacex-s1"
MICRON_DEMO_SOURCE_ID = "demo-financial-micron-report-530bd7ed"
TRANSFORMERS_DEMO_SOURCE_ID = "demo-stem-transformers-tutorial"


class FakeResultStorage:
    def __init__(self) -> None:
        self.raw_files_by_job_id: dict[str, set[str]] = {}

    def upload(
        self,
        *,
        job_id: str,
        result_dir: str,
        zip_file_path: str,
    ) -> SimpleNamespace:
        assert Path(zip_file_path).is_file()
        result_path = Path(result_dir)
        raw_files = {
            file_path.relative_to(result_path).as_posix()
            for file_path in result_path.rglob("*")
            if file_path.is_file()
        }
        self.raw_files_by_job_id[job_id] = raw_files
        return SimpleNamespace(
            zip_key=f"results/{job_id}.zip",
            raw_prefix=f"results/{job_id}/",
            raw_files={
                raw_file: f"results/{job_id}/{raw_file}"
                for raw_file in sorted(raw_files)
            },
        )

    def normalize_artifact_ref(self, artifact_ref: str | None) -> str | None:
        if not artifact_ref:
            return None
        normalized = str(artifact_ref).strip().replace("\\", "/").lstrip("/")
        if (
            normalized.startswith("images/")
            or normalized.startswith("tables/")
            or normalized.startswith("page_citation_assets/")
        ):
            return normalized
        return None

    def generate_artifact_url(
        self,
        *,
        job_id: str,
        artifact_ref: str,
        expires_in: int = 3600,
    ) -> str | None:
        normalized = self.normalize_artifact_ref(artifact_ref)
        if not normalized:
            return None
        if normalized not in self.raw_files_by_job_id.get(job_id, set()):
            return None
        return f"https://assets.example.test/{job_id}/{normalized}"


def _load_source_catalog_module() -> ModuleType:
    configure_import_environment()
    ensure_import_paths()
    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            sys.modules.pop(module_name, None)
    return importlib.import_module("app.services.demo.source_catalog")


def test_should_keep_demo_catalog_metadata_light_for_sources_without_examples(
    monkeypatch: MonkeyPatch,
) -> None:
    source_catalog_module = _load_source_catalog_module()
    loaded_demo_source_ids: list[str] = []
    original_load_source_chunks = source_catalog_module._load_source_chunks

    def load_source_chunks(
        source: Any,
    ) -> tuple[dict[str, Any], ...]:
        loaded_demo_source_ids.append(source.demo_source_id)
        return original_load_source_chunks(source)

    monkeypatch.setattr(
        source_catalog_module,
        "_load_source_chunks",
        load_source_chunks,
    )

    catalog = source_catalog_module.DemoSourceCatalog().get_catalog()

    assert catalog["sources"]
    assert DEMO_SOURCE_ID in loaded_demo_source_ids
    assert MICRON_DEMO_SOURCE_ID not in loaded_demo_source_ids
    assert TRANSFORMERS_DEMO_SOURCE_ID not in loaded_demo_source_ids


def test_should_project_demo_example_cite_markers_and_page_numbers() -> None:
    catalog = _load_source_catalog_module().DemoSourceCatalog().get_catalog()
    sources = {
        str(source["demo_source_id"]): source
        for source in catalog["sources"]
    }
    spacex = sources[DEMO_SOURCE_ID]
    spacex_example = spacex["examples"][0]
    spacex_citation = spacex_example["citations"][0]

    assert "[[cite:1]]" in spacex_example["answer"]
    assert spacex_citation["page_citation_page_number"] == 28
    assert spacex_citation["page_nums"] == [28]
    assert str(spacex_citation["source"]["section_path"]).endswith("/Root")


def test_should_preserve_filename_rooted_sections_when_publishing_demo_chunks() -> None:
    source_catalog_module = _load_source_catalog_module()
    catalog = source_catalog_module.DemoSourceCatalog()
    source = catalog.require_source(MICRON_DEMO_SOURCE_ID)
    publication_chunks = catalog.publication_chunks(source)
    publication_paths = {
        str(chunk.get("path") or "")
        for chunk in publication_chunks
        if chunk.get("type") == "page"
    }

    assert f"{source.title}/Root/Appendix" in publication_paths
    assert f"{source.title}/Root/AI use at Micron" in publication_paths
