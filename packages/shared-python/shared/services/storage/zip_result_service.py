"""
ZIP Result Package Generation Service
Generates ZIP packages according to Knowhere-API-ZIP-Spec.md specification
"""

import hashlib
import json
import os
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from PIL import Image

import pandas as pd

from shared.core.exceptions.domain_exceptions import (
    KnowhereException,
    StorageServiceException,
)
from shared.services.storage.zip_result_schema import ZipResultSchemaBuilder


class ZipResultService:
    """ZIP Result Package Generation Service"""

    def __init__(self):
        self._schema = ZipResultSchemaBuilder()

    def generate_zip_package(
        self,
        job_id: str,
        chunks: List[Dict[str, Any]],
        add_dir: str,
        source_file_name: str,
        data_id: Optional[str],
        job_metadata: Dict[str, Any],
        parsed_df: Optional["pd.DataFrame"] = None,
        temp_dir: Optional[str] = None,
    ) -> Tuple[str, Dict[str, str], Dict[str, Any], int]:
        """
        Generate ZIP result package

        Args:
            job_id: Job ID
            chunks: List of chunks data
            add_dir: Parsed directory path (contains images/ and tables/ directories)
            source_file_name: Source file name
            data_id: User-defined ID
            job_metadata: Job metadata
            parsed_df: Optional, parsed DataFrame (legacy, unused after doc_nav.json migration)
            temp_dir: Optional directory for the generated ZIP file

        Returns:
            Tuple[zip_file_path, checksum, statistics, zip_size]:
            - zip_file_path: ZIP file path
            - checksum: {"algorithm": "sha256", "value": "..."}
            - statistics: {"total_chunks": int, "text_chunks": int, "image_chunks": int, "table_chunks": int, "total_pages": Optional[int]}
            - zip_size: ZIP file size in bytes
        """
        try:
            # Create temporary ZIP file
            effective_temp_dir = temp_dir or tempfile.gettempdir()
            os.makedirs(effective_temp_dir, exist_ok=True)
            zip_file_path = os.path.join(effective_temp_dir, f"result_{job_id}.zip")

            # Collect image and table file info (must be done before formatting chunks as file info is needed)
            images_dir = os.path.join(add_dir, "images")
            tables_dir = os.path.join(add_dir, "tables")
            image_files_info = self._collect_image_files(chunks, images_dir)
            table_files_info = self._collect_table_files(chunks, tables_dir)

            # Create image and table file mappings (chunk_id -> file_info)
            image_files_map = {img["id"]: img for img in image_files_info}
            table_files_map = {tb["id"]: tb for tb in table_files_info}

            # Convert chunks data format (using file info)
            formatted_chunks = self._schema.format_chunks(
                chunks, image_files_map, table_files_map
            )
            statistics = self._schema.calculate_statistics(formatted_chunks)

            doc_nav: Dict[str, Any] = {}
            hierarchy: Dict[str, Any] = {}

            # Create ZIP package
            with zipfile.ZipFile(zip_file_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
                # 1. Generate chunks.json (full version)
                chunks_json = json.dumps(
                    {"chunks": formatted_chunks}, ensure_ascii=False, indent=2
                )
                zip_file.writestr("chunks.json", chunks_json.encode("utf-8"))

                # 2. Try to add full.md (if exists)
                full_md_path = os.path.join(add_dir, "full.md")
                if os.path.exists(full_md_path):
                    zip_file.write(full_md_path, "full.md")

                # 2b. Try to add toc_hierarchies.json (if exists)
                toc_path = os.path.join(add_dir, "toc_hierarchies.json")
                if os.path.exists(toc_path):
                    zip_file.write(toc_path, "toc_hierarchies.json")
                    logger.info("Added toc_hierarchies.json to ZIP")

                # 3. Add image files
                for img_info in image_files_info:
                    source_path = img_info["source_path"]
                    if os.path.exists(source_path):
                        zip_file.write(
                            source_path,
                            img_info["zip_path"],
                        )
                    else:
                        logger.warning(f"Image file not found: {source_path}")

                # 4. Add table files
                for table_info in table_files_info:
                    source_path = table_info["source_path"]
                    if os.path.exists(source_path):
                        zip_file.write(
                            source_path,
                            table_info["zip_path"],
                        )
                    else:
                        logger.warning(f"Table file not found: {source_path}")

                # 5. Generate doc_nav.json — unified navigation file
                try:
                    doc_nav = self._schema.build_doc_nav(formatted_chunks, source_file_name)
                    hierarchy = self._schema.build_hierarchy_dict(doc_nav.get("sections", []))
                    doc_nav_json = json.dumps(doc_nav, ensure_ascii=False, indent=2)
                    zip_file.writestr("doc_nav.json", doc_nav_json.encode("utf-8"))
                    logger.info("Added doc_nav.json")
                except Exception as e:
                    logger.warning(f"generate doc_nav.json fail {e}")

                # 6. Generate manifest.json (checksum not included, stored in database)
                manifest = self._schema.generate_manifest(
                    job_id=job_id,
                    data_id=data_id,
                    source_file_name=source_file_name,
                    statistics=statistics,
                    job_metadata=job_metadata,
                    hierarchy=hierarchy,
                )
                manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)
                zip_file.writestr("manifest.json", manifest_json.encode("utf-8"))

            # Calculate ZIP package SHA-256
            checksum_value = self._calculate_zip_checksum(zip_file_path)
            checksum = {"algorithm": "sha256", "value": checksum_value}

            # Get ZIP file size
            zip_size = os.path.getsize(zip_file_path)

            logger.info(
                f"ZIP package generated successfully: job_id={job_id}, size={zip_size}, checksum={checksum_value[:16]}..."
            )

            return zip_file_path, checksum, statistics, zip_size

        except KnowhereException:
            raise
        except Exception as e:
            logger.error(f"Failed to generate ZIP package: {e}")
            raise StorageServiceException(
                internal_message=f"Failed to generate ZIP package: {str(e)}",
                operation="generate_zip_package",
                original_exception=e,
            )

    def _collect_image_files(
        self, chunks: List[Dict[str, Any]], images_dir: str
    ) -> List[Dict[str, Any]]:
        """Collect image file information"""
        image_files = []
        if not os.path.exists(images_dir):
            has_image_chunks = any(chunk.get("type", "") == "image" for chunk in chunks)
            if has_image_chunks:
                raise StorageServiceException(
                    internal_message=(
                        "Image directory not found for ZIP packaging: "
                        f"images_dir={images_dir}"
                    ),
                    operation="collect_image_files",
                )
            return image_files

        # Get all image files
        image_files_map = {}
        for filename in os.listdir(images_dir):
            file_path = os.path.join(images_dir, filename)
            if os.path.isfile(file_path):
                image_files_map[filename] = file_path

        def add_candidate(candidates: List[str], value: Optional[str]) -> None:
            if not value:
                return
            candidate = os.path.basename(str(value).strip().replace("-->", "/"))
            if not candidate:
                return
            if candidate.startswith("[") and candidate.endswith("]"):
                candidate = candidate[1:-1].strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        def resolve_source_path(
            candidates: List[str], chunk_id: str
        ) -> Tuple[Optional[str], Optional[str], str]:
            for candidate in candidates:
                if candidate in image_files_map:
                    _, ext = os.path.splitext(candidate)
                    return (
                        image_files_map[candidate],
                        candidate,
                        ext.lstrip(".") or "jpg",
                    )

                stem, ext = os.path.splitext(candidate)
                if stem:
                    stem_matches = [
                        filename
                        for filename in image_files_map
                        if os.path.splitext(filename)[0] == stem
                    ]
                    if len(stem_matches) == 1:
                        matched = stem_matches[0]
                        _, matched_ext = os.path.splitext(matched)
                        return (
                            image_files_map[matched],
                            matched,
                            matched_ext.lstrip(".") or "jpg",
                        )

            return None, None, "jpg"

        # Match images from chunks
        for chunk in chunks:
            chunk_id = chunk.get("chunk_id") or chunk.get("know_id")
            chunk_type = chunk.get("type", "")

            if chunk_type != "image":
                continue

            metadata = chunk.get("metadata", {})
            candidate_names: List[str] = []
            metadata_file_path = ""
            if metadata and isinstance(metadata, dict):
                metadata_file_path = str(metadata.get("file_path") or "").strip()
                add_candidate(candidate_names, metadata.get("file_path"))
                add_candidate(candidate_names, metadata.get("original_name"))

            # Try to get original filename from chunk's path field
            if metadata_file_path.startswith("images/"):
                original_name = os.path.basename(metadata_file_path)
            else:
                original_name = None

            original_path = chunk.get("path", "")
            if original_path:
                add_candidate(candidate_names, original_path)
                # Normalize path separators: replace --> with /, then extract filename
                normalized_path = original_path.replace("-->", "/")
                original_name = os.path.basename(normalized_path)

            source_path, matched_name, ext = resolve_source_path(
                candidate_names, str(chunk_id)
            )
            if matched_name:
                original_name = matched_name

            if not source_path:
                raise StorageServiceException(
                    internal_message=(
                        "Cannot resolve image file for ZIP packaging: "
                        f"chunk_id={chunk_id}, candidates={candidate_names}"
                    ),
                    operation="collect_image_files",
                )

            # Get image dimensions
            width = None
            height = None
            try:
                with Image.open(source_path) as img:
                    width, height = img.size
            except Exception:
                pass

            file_size = os.path.getsize(source_path)

            # Priority: use file_path from metadata, then original_name, finally chunk_id
            if metadata and isinstance(metadata, dict):
                # metadata.file_path format: "images/xxx.jpg"
                zip_file_path = metadata.get("file_path")
                if zip_file_path and zip_file_path.startswith("images/"):
                    # Use complete path from metadata
                    zip_path = zip_file_path
                    # Extract filename as original_name
                    if not original_name:
                        original_name = metadata.get(
                            "original_name"
                        ) or os.path.basename(zip_file_path)
                else:
                    # If metadata has no file_path, use original_name or chunk_id
                    if original_name:
                        zip_path = f"images/{original_name}"
                    else:
                        zip_path = f"images/{chunk_id}.{ext}"
            else:
                # If no metadata, use original_name or chunk_id
                if original_name:
                    zip_path = f"images/{original_name}"
                else:
                    zip_path = f"images/{chunk_id}.{ext}"

            image_files.append(
                {
                    "id": str(chunk_id),
                    "file_path": zip_path,
                    "original_name": original_name or f"image_{chunk_id}.{ext}",
                    "size_bytes": file_size,
                    "format": ext.lower(),
                    "width": width,
                    "height": height,
                    "source_path": source_path,
                    "zip_path": zip_path,
                }
            )

        return image_files

    def _collect_table_files(
        self, chunks: List[Dict[str, Any]], tables_dir: str
    ) -> List[Dict[str, Any]]:
        """Collect table file information"""
        table_files = []
        if not os.path.exists(tables_dir):
            return table_files

        # Get all table files
        table_files_map = {}
        for filename in os.listdir(tables_dir):
            file_path = os.path.join(tables_dir, filename)
            if os.path.isfile(file_path) and filename.endswith(".html"):
                table_files_map[filename] = file_path

        def add_candidate(candidates: List[str], value: Optional[str]) -> None:
            if not value:
                return
            candidate = os.path.basename(str(value).strip().replace("-->", "/"))
            if not candidate:
                return
            if candidate.startswith("[") and candidate.endswith("]"):
                candidate = candidate[1:-1].strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        def resolve_source_path(
            candidates: List[str],
        ) -> Tuple[Optional[str], Optional[str]]:
            for candidate in candidates:
                if candidate in table_files_map:
                    return table_files_map[candidate], candidate

                stem, _ = os.path.splitext(candidate)
                if stem:
                    stem_matches = [
                        filename
                        for filename in table_files_map
                        if os.path.splitext(filename)[0] == stem
                    ]
                    if len(stem_matches) == 1:
                        matched = stem_matches[0]
                        return table_files_map[matched], matched

            return None, None

        # Match tables from chunks
        for chunk in chunks:
            chunk_id = chunk.get("chunk_id") or chunk.get("know_id")
            chunk_type = chunk.get("type", "")

            if chunk_type != "table":
                continue

            metadata = chunk.get("metadata", {})
            candidate_names: List[str] = []
            if metadata and isinstance(metadata, dict):
                add_candidate(candidate_names, metadata.get("file_path"))
                add_candidate(candidate_names, metadata.get("original_name"))

            # Try to get original filename from chunk's path field
            original_path = chunk.get("path", "")
            if original_path:
                # Normalize path separators: replace --> with /, then extract filename
                normalized_path = original_path.replace("-->", "/")
                original_name = os.path.basename(normalized_path)
                add_candidate(candidate_names, original_path)
            else:
                original_name = None

            source_path, matched_name = resolve_source_path(candidate_names)
            if matched_name:
                original_name = matched_name

            if not source_path:
                raise StorageServiceException(
                    internal_message=(
                        "Cannot resolve table file for ZIP packaging: "
                        f"chunk_id={chunk_id}, candidates={candidate_names}"
                    ),
                    operation="collect_table_files",
                )

            file_size = os.path.getsize(source_path)

            # Priority: use file_path from metadata, then original_name, finally chunk_id
            if metadata and isinstance(metadata, dict):
                # metadata.file_path format: "tables/xxx.html"
                zip_file_path = metadata.get("file_path")
                if zip_file_path and zip_file_path.startswith("tables/"):
                    # Use complete path from metadata
                    zip_path = zip_file_path
                    # Extract filename as original_name
                    if not original_name:
                        original_name = metadata.get(
                            "original_name"
                        ) or os.path.basename(zip_file_path)
                else:
                    # If metadata has no file_path, use original_name or chunk_id
                    if original_name:
                        zip_path = f"tables/{original_name}"
                    else:
                        zip_path = f"tables/{chunk_id}.html"
            else:
                # If no metadata, use original_name or chunk_id
                if original_name:
                    zip_path = f"tables/{original_name}"
                else:
                    zip_path = f"tables/{chunk_id}.html"

            table_files.append(
                {
                    "id": str(chunk_id),
                    "file_path": zip_path,
                    "original_name": original_name or f"table_{chunk_id}.html",
                    "size_bytes": file_size,
                    "format": "html",
                    "source_path": source_path,
                    "zip_path": zip_path,
                }
            )

        return table_files

    def _calculate_zip_checksum(self, zip_file_path: str) -> str:
        """Calculate SHA-256 checksum of ZIP file"""
        sha256_hash = hashlib.sha256()
        with open(zip_file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest().lower()
