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

from shared.services.chunks.chunk_connections import (
    build_resource_target_map,
    convert_refs_to_embed_connections,
    merge_connections,
    normalize_connect_to_targets,
    parse_relationship_refs,
)
from shared.utils.text_utils import truncate_content_preview

import pandas as pd

from shared.core.exceptions.domain_exceptions import (
    KnowhereException,
    StorageServiceException,
)
from shared.utils.utc_now import utc_now_naive


class ZipResultService:
    """ZIP Result Package Generation Service"""

    def __init__(self):
        pass

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
            formatted_chunks = self._format_chunks(
                chunks, image_files_map, table_files_map
            )
            statistics = self._calculate_statistics(formatted_chunks)

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
                    doc_nav = self._build_doc_nav(formatted_chunks, source_file_name)
                    hierarchy = self._build_hierarchy_dict(doc_nav.get("sections", []))
                    doc_nav_json = json.dumps(doc_nav, ensure_ascii=False, indent=2)
                    zip_file.writestr("doc_nav.json", doc_nav_json.encode("utf-8"))
                    logger.info("Added doc_nav.json")
                except Exception as e:
                    logger.warning(f"generate doc_nav.json fail {e}")

                # 6. Generate manifest.json (checksum not included, stored in database)
                manifest = self._generate_manifest(
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

    def _calculate_statistics(self, chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate statistics"""
        total_chunks = len(chunks)
        text_chunks = 0
        image_chunks = 0
        table_chunks = 0

        for chunk in chunks:
            chunk_type = chunk.get("type", "")
            raw_type = str(chunk_type).strip()
            normalized_type = raw_type.split("\n", 1)[0].lower()
            if normalized_type == "image":
                image_chunks += 1
            elif normalized_type == "table":
                table_chunks += 1
            else:
                text_chunks += 1

        return {
            "total_chunks": total_chunks,
            "text_chunks": text_chunks,
            "image_chunks": image_chunks,
            "table_chunks": table_chunks,
            "total_pages": None,  # Cannot determine page count at this point
        }

    def _format_chunks(
        self,
        chunks: List[Dict[str, Any]],
        image_files_map: Dict[str, Dict[str, Any]],
        table_files_map: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Convert chunks data to ZIP specification format"""
        resource_target_map = build_resource_target_map(
            chunks,
            image_files_map=image_files_map,
            table_files_map=table_files_map,
        )

        formatted = []
        for chunk in chunks:
            chunk_id = str(chunk.get("chunk_id") or chunk.get("know_id"))
            chunk_type_str = chunk.get("type", "")
            raw_type = str(chunk_type_str).strip()
            normalized_type = raw_type.split("\n", 1)[0].lower()
            img_info = image_files_map.get(chunk_id)

            # Determine chunk type
            if normalized_type == "image":
                chunk_type = "image"
            elif normalized_type == "table":
                chunk_type = "table"
            else:
                chunk_type = "text"

            # Get content
            content = chunk.get("text") or chunk.get("content", "")

            # Use original path directly to match kb.csv
            path = chunk.get("path", "")

            # Get or build base metadata
            existing_metadata = chunk.get("metadata", {})
            metadata = {
                "length": existing_metadata.get("length") or len(content),
                "summary": existing_metadata.get("summary") or chunk.get("summary", ""),
                "page_nums": existing_metadata.get("page_nums", []),
            }
            document_top_summary = str(
                existing_metadata.get("document_top_summary") or ""
            ).strip()
            if document_top_summary:
                metadata["document_top_summary"] = document_top_summary

            # Add type-specific fields
            if chunk_type == "text":
                metadata["tokens"] = existing_metadata.get("tokens") or chunk.get(
                    "tokens", 0
                )
                metadata["keywords"] = existing_metadata.get("keywords") or chunk.get(
                    "keywords", []
                )

                # Convert in-text resource refs into embeds edges.
                relationship_refs = parse_relationship_refs(
                    chunk.get("type_raw") or chunk_type_str,
                    str(content),
                )
                embed_connections = convert_refs_to_embed_connections(
                    relationship_refs, resource_target_map
                )
                related_connections = normalize_connect_to_targets(
                    existing_metadata.get("connect_to")
                    or chunk.get("connect_to")
                    or chunk.get("connectto"),
                    resource_target_map,
                )
                metadata["connect_to"] = merge_connections(
                    embed_connections, related_connections
                )

            elif chunk_type == "image":
                if img_info:
                    metadata["file_path"] = img_info["file_path"]
                # Unified schema: include keywords and tokens for all chunk types
                metadata["keywords"] = existing_metadata.get("keywords") or chunk.get(
                    "keywords", []
                )
                metadata["tokens"] = []

            elif chunk_type == "table":
                # Get table info from existing_metadata or table_files_map
                file_path = existing_metadata.get("file_path")

                if not file_path:
                    # Get table info from table_files_map
                    tb_info = table_files_map.get(chunk_id)
                    if tb_info:
                        file_path = tb_info["file_path"]
                    else:
                        # Extract from path or use default
                        tbl_name = (
                            path.split("/")[-1]
                            if "/" in path
                            else f"table_{chunk_id}.html"
                        )
                        file_path = f"tables/{tbl_name}"

                metadata["file_path"] = file_path
                # Unified schema: include keywords and tokens for all chunk types
                metadata["keywords"] = existing_metadata.get("keywords") or chunk.get(
                    "keywords", []
                )
                metadata["tokens"] = []

            formatted_chunk = {
                "chunk_id": chunk_id,
                "type": chunk_type,
                "content": content,
                "path": path,
                "metadata": metadata,
            }
            formatted.append(formatted_chunk)

        return formatted

    def _clean_path(self, path: str) -> str:
        """Clean path, keep only logical path"""
        if not path:
            return "/"

        # Remove filesystem path prefix
        # Example: .-->users-->KB_DATA_xxx-->dir-->file.pdf-->chapter-->section
        # Should extract: chapter-->section

        # Find the last .pdf, .docx, etc. file extension
        import re

        # Match filename pattern (with extension)
        file_pattern = r"[^/]+\.(pdf|docx|doc|txt|md|xlsx|xls|pptx|ppt)"
        match = re.search(file_pattern, path, re.IGNORECASE)

        if match:
            # Extract the part after filename
            path_after_file = path[match.end() :]
            # Clean path separators
            path_after_file = path_after_file.replace("-->", "/").strip("/")
            if path_after_file:
                return path_after_file

        # If no file pattern found, try to clean common prefixes
        path = path.replace("-->", "/")
        # Remove leading path separators and empty segments
        path = "/".join(
            [p for p in path.split("/") if p and p not in ["", ".", "users"]]
        )
        return path if path else "/"

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

    def _generate_manifest(
        self,
        job_id: str,
        data_id: Optional[str],
        source_file_name: str,
        statistics: Dict[str, Any],
        job_metadata: Dict[str, Any],
        hierarchy: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate manifest.json"""
        manifest = {
            "version": "2.0",
            "job_id": job_id,
            "data_id": data_id,
            "source_file_name": source_file_name,
            "processing_date": utc_now_naive().isoformat() + "Z",
            "processing": {
                "page_count": job_metadata.get("page_count"),
                "billing_status": job_metadata.get("billing_status"),
                "cost": {
                    "micro_dollars": job_metadata.get("billing_amount_micro_dollars"),
                    "credits": job_metadata.get("billing_credits"),
                },
                "timing": {
                    "started_at": job_metadata.get("processing_started_at"),
                    "completed_at": job_metadata.get("processing_completed_at"),
                    "duration_ms": job_metadata.get("processing_duration_ms"),
                },
            },
            "statistics": statistics,
            "HIERARCHY": hierarchy or {},
        }

        return manifest

    def _calculate_zip_checksum(self, zip_file_path: str) -> str:
        """Calculate SHA-256 checksum of ZIP file"""
        sha256_hash = hashlib.sha256()
        with open(zip_file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest().lower()

    def _build_hierarchy_dict(
        self,
        sections: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build a title-only nested hierarchy from doc_nav sections."""
        hierarchy: Dict[str, Any] = {}
        title_counts: Dict[str, int] = {}

        for section in sections:
            raw_title = str(section.get("title") or "").strip()
            if not raw_title:
                continue

            title_counts[raw_title] = title_counts.get(raw_title, 0) + 1
            title = (
                raw_title
                if title_counts[raw_title] == 1
                else f"{raw_title} ({title_counts[raw_title]})"
            )
            hierarchy[title] = self._build_hierarchy_dict(
                section.get("children") or []
            )

        return hierarchy

    def _build_doc_nav(
        self,
        formatted_chunks: List[Dict[str, Any]],
        source_file_name: str,
    ) -> Dict[str, Any]:
        """Build doc_nav.json — unified navigation file.

        Structured file serving both human demo and LLM navigation.

        The output contains:
        - ``sections``: tree of text sections with summaries and chunk counts.
        - ``resources``: flat lists of image/table chunks with summaries.
        - ``stats``: chunk counts by type.

        Each leaf section carries a ``summary`` derived from:
          1. chunk.metadata.summary (LLM-generated, highest quality)
          2. chunk.content[:300] (fallback truncation)

        Non-leaf section summaries are left empty at this stage and are
        populated later by ``summary_builder.enrich_doc_nav_summaries``.
        """
        # ── Separate text chunks from resource chunks ──
        text_chunks: List[Dict[str, Any]] = []
        image_resources: List[Dict[str, Any]] = []
        table_resources: List[Dict[str, Any]] = []

        stats = {"total_chunks": 0, "text_chunks": 0, "image_chunks": 0, "table_chunks": 0, "max_depth": 0}

        for fc in formatted_chunks:
            ctype = fc.get("type", "text")
            path = fc.get("path", "")
            meta = fc.get("metadata") or {}
            summary_raw = (meta.get("summary") or "").strip()
            content_raw = (fc.get("content") or "").strip()
            # Normalize whitespace
            summary = " ".join(summary_raw.split()) if summary_raw else ""
            content_preview = truncate_content_preview(content_raw) if content_raw else ""

            stats["total_chunks"] += 1

            if ctype == "image":
                stats["image_chunks"] += 1
                image_resources.append({
                    "path": path,
                    "summary": summary or content_preview,
                })
            elif ctype == "table":
                stats["table_chunks"] += 1
                table_resources.append({
                    "path": path,
                    "summary": summary or content_preview,
                })
            else:
                stats["text_chunks"] += 1
                text_chunks.append({
                    "path": path,
                    "summary": summary or content_preview,
                })

        # ── Build section tree from text chunk paths ──
        # Each text chunk path looks like: "kb_root/filename.pdf/Section/Subsection"
        # We strip the kb_root and filename prefix to get relative section paths.
        sections = self._build_section_tree(text_chunks)

        # Compute max depth
        def _max_depth(nodes: list, d: int = 1) -> int:
            m = d if nodes else 0
            for n in nodes:
                m = max(m, _max_depth(n.get("children", []), d + 1))
            return m

        stats["max_depth"] = _max_depth(sections)

        return {
            "version": "1.0",
            "file_name": source_file_name or "",
            "stats": stats,
            "sections": sections,
            "resources": {
                "images": image_resources,
                "tables": table_resources,
            },
        }

    def _build_section_tree(
        self,
        text_chunks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build a tree of sections from flat text chunk paths.

        Each text chunk has a ``path`` like ``"kb/file.pdf/Sec1/Sub1"``.
        We extract section parts (after kb_root + filename) and build a
        tree using ``children`` arrays.

        Returns a list of top-level section nodes.
        """
        # Internal tree node: {title, summary, chunk_count, children: {title: node}}
        root_children: Dict[str, dict] = {}  # ordered dict of top-level titles

        for chunk in text_chunks:
            path = chunk.get("path", "")
            parts = [p.strip() for p in path.split("/") if p.strip()]
            # Skip kb_root + filename → section parts start at index 2
            section_parts = parts[2:] if len(parts) > 2 else []

            if not section_parts:
                # Root-level chunk (no section hierarchy)
                key = "__root__"
                if key not in root_children:
                    root_children[key] = {
                        "title": "Root",
                        "path": "/".join(parts[:2]) if len(parts) >= 2 else path,
                        "summary": chunk.get("summary", ""),
                        "chunk_count": 0,
                        "_children_map": {},
                    }
                root_children[key]["chunk_count"] += 1
                # Use the first chunk's summary for root
                if not root_children[key]["summary"]:
                    root_children[key]["summary"] = chunk.get("summary", "")
                continue

            # Walk the tree, creating nodes as needed
            current_level = root_children
            full_section_path_parts = parts[:2]  # start with kb_root/filename
            for i, part in enumerate(section_parts):
                full_section_path_parts.append(part)
                if part not in current_level:
                    current_level[part] = {
                        "title": part,
                        "path": "/".join(full_section_path_parts),
                        "summary": "",
                        "chunk_count": 0,
                        "_children_map": {},
                    }
                node = current_level[part]
                if i == len(section_parts) - 1:
                    # Leaf — this is the chunk's actual section
                    node["chunk_count"] += 1
                    if not node["summary"]:
                        node["summary"] = chunk.get("summary", "")
                current_level = node["_children_map"]

        # Convert internal tree to output format
        def _to_output(children_map: Dict[str, dict], level: int = 1) -> List[Dict[str, Any]]:
            result = []
            for node in children_map.values():
                children = _to_output(node["_children_map"], level + 1)
                # Compute total chunk_count including descendants
                total_chunks = node["chunk_count"] + sum(
                    c.get("chunk_count", 0) for c in children
                )
                out = {
                    "title": node["title"],
                    "path": node["path"],
                    "level": level,
                    "summary": node["summary"],
                    "chunk_count": total_chunks,
                    "children": children,
                }
                result.append(out)
            return result

        return _to_output(root_children)
