"""
ZIP Result Package Generation Service.

Generates ZIP packages according to the Knowhere API ZIP result format.
"""

from __future__ import annotations

import json
import os
from typing import Any

from loguru import logger

import pandas as pd

from shared.core.exceptions.domain_exceptions import (
    KnowhereException,
    StorageServiceException,
)
from shared.services.storage.zip_package_writer import (
    ZipPackageWriter,
    ZipPackageWriteRequest,
)
from shared.services.storage.zip_result_resources import ZipResourceCollector
from shared.services.storage.zip_result_schema import ZipResultSchemaBuilder
from shared.services.storage.zip_manifest_schema import strip_manifest_cost_fields


class ZipResultService:
    """Generate a result ZIP while preserving the worker-facing package contract."""

    def __init__(
        self,
        *,
        schema_builder: ZipResultSchemaBuilder | None = None,
        resource_collector: ZipResourceCollector | None = None,
        package_writer: ZipPackageWriter | None = None,
    ) -> None:
        self._schema = schema_builder or ZipResultSchemaBuilder()
        self._resources = resource_collector or ZipResourceCollector()
        self._writer = package_writer or ZipPackageWriter()

    def generate_zip_package(
        self,
        job_id: str,
        chunks: list[dict[str, Any]],
        add_dir: str,
        source_file_name: str,
        data_id: str | None,
        job_metadata: dict[str, Any],
        parsed_df: pd.DataFrame | None = None,
        temp_dir: str | None = None,
    ) -> tuple[str, dict[str, str], dict[str, Any], int]:
        """
        Generate ZIP result package.

        Returns:
            Tuple[zip_file_path, checksum, statistics, zip_size]:
            - zip_file_path: ZIP file path
            - checksum: {"algorithm": "sha256", "value": "..."}
            - statistics: chunk and page statistics persisted with the Job Result
            - zip_size: ZIP file size in bytes
        """
        try:
            resources = self._resources.collect(chunks=chunks, add_dir=add_dir)
            formatted_chunks = self._schema.format_chunks(
                chunks,
                resources.image_files_map,
                resources.table_files_map,
            )
            statistics = self._schema.calculate_statistics(formatted_chunks)

            doc_nav, hierarchy = self._build_navigation_outputs(
                add_dir=add_dir,
                formatted_chunks=formatted_chunks,
                source_file_name=source_file_name,
            )
            manifest = self._schema.generate_manifest(
                job_id=job_id,
                data_id=data_id,
                source_file_name=source_file_name,
                statistics=statistics,
                job_metadata=job_metadata,
                hierarchy=hierarchy,
            )
            manifest = strip_manifest_cost_fields(manifest)
            parse_track = str((job_metadata or {}).get("parse_track") or "")
            artifact = self._writer.write(
                ZipPackageWriteRequest(
                    job_id=job_id,
                    add_dir=add_dir,
                    formatted_chunks=formatted_chunks,
                    image_files=resources.image_files,
                    table_files=resources.table_files,
                    page_citation_files=resources.page_citation_files,
                    doc_nav=doc_nav,
                    manifest=manifest,
                    temp_dir=temp_dir,
                    include_toc_hierarchies=parse_track != "page_memory",
                )
            )

            logger.info(
                "ZIP package generated successfully: "
                f"job_id={job_id}, size={artifact.zip_size}, "
                f"checksum={artifact.checksum['value'][:16]}..."
            )

            return (
                artifact.zip_file_path,
                artifact.checksum,
                statistics,
                artifact.zip_size,
            )

        except KnowhereException:
            raise
        except Exception as exc:
            logger.error(f"Failed to generate ZIP package: {exc}")
            raise StorageServiceException(
                internal_message=f"Failed to generate ZIP package: {str(exc)}",
                operation="generate_zip_package",
                original_exception=exc,
            )

    def _build_navigation_outputs(
        self,
        *,
        add_dir: str,
        formatted_chunks: list[dict[str, Any]],
        source_file_name: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Prefer the already-enriched on-disk doc_nav; rebuild only as fallback."""
        try:
            existing = self._load_existing_doc_nav(add_dir)
            if existing is not None:
                hierarchy = self._schema.build_hierarchy_dict(
                    existing.get("sections", [])
                )
                logger.info("Using enriched on-disk doc_nav.json for ZIP package")
                return existing, hierarchy

            doc_nav = self._schema.build_doc_nav(formatted_chunks, source_file_name)
            hierarchy = self._schema.build_hierarchy_dict(doc_nav.get("sections", []))
            return doc_nav, hierarchy
        except Exception as exc:
            logger.warning(f"generate doc_nav.json fail {exc}")
            return None, {}

    @staticmethod
    def _load_existing_doc_nav(add_dir: str) -> dict[str, Any] | None:
        if not add_dir:
            return None
        path = os.path.join(add_dir, "doc_nav.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            logger.warning(f"Failed to read existing doc_nav.json: {exc}")
            return None
        if not isinstance(payload, dict):
            return None
        if not isinstance(payload.get("sections"), list):
            return None
        return payload
