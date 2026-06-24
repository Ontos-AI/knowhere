"""Schema projection facade for Knowhere ZIP result packages."""

from __future__ import annotations

from typing import Any

from shared.services.storage.zip_chunk_schema import ZipChunkSchemaBuilder
from shared.services.storage.zip_doc_navigation import ZipDocNavigationBuilder
from shared.services.storage.zip_manifest_schema import ZipManifestBuilder


class ZipResultSchemaBuilder:
    def __init__(
        self,
        *,
        chunk_schema: ZipChunkSchemaBuilder | None = None,
        doc_navigation: ZipDocNavigationBuilder | None = None,
        manifest_builder: ZipManifestBuilder | None = None,
    ) -> None:
        self._chunk_schema = chunk_schema or ZipChunkSchemaBuilder()
        self._doc_navigation = doc_navigation or ZipDocNavigationBuilder()
        self._manifest_builder = manifest_builder or ZipManifestBuilder()

    def calculate_statistics(self, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        return self._chunk_schema.calculate_statistics(chunks)

    def format_chunks(
        self,
        chunks: list[dict[str, Any]],
        image_files_map: dict[str, dict[str, Any]],
        table_files_map: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return self._chunk_schema.format_chunks(
            chunks,
            image_files_map,
            table_files_map,
        )

    def generate_manifest(
        self,
        *,
        job_id: str,
        data_id: str | None,
        source_file_name: str,
        statistics: dict[str, Any],
        job_metadata: dict[str, Any],
        hierarchy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._manifest_builder.generate_manifest(
            job_id=job_id,
            data_id=data_id,
            source_file_name=source_file_name,
            statistics=statistics,
            job_metadata=job_metadata,
            hierarchy=hierarchy,
        )

    def build_hierarchy_dict(
        self,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._doc_navigation.build_hierarchy_dict(sections)

    def build_doc_nav(
        self,
        formatted_chunks: list[dict[str, Any]],
        source_file_name: str,
    ) -> dict[str, Any]:
        return self._doc_navigation.build_doc_nav(
            formatted_chunks,
            source_file_name,
        )
