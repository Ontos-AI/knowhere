from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_ingestion.parse_result_package import (
    build_parse_result_package,
)
from app.services.document_parser.orchestration.parse_output import ParseOutput
from shared.models.database.document import DocumentSection
from shared.services.chunks.canonical_chunk_builder import rows_to_chunks
from shared.services.retrieval.publication_content import (
    _build_document_chunk,
    _get_chunk_metadata,
)
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.storage.zip_chunk_schema import ZipChunkSchemaBuilder


def test_page_memory_chunks_stay_aligned_through_zip_and_publication(tmp_path) -> None:
    canonical_chunks = rows_to_chunks(
        [
            {
                "content": "owned body",
                "path": "demo.pdf/Section",
                "type": "page",
                "length": 10,
                "keywords": "Acme",
                "summary": "Page summary",
                "know_id": "node_owner",
                "tokens": "",
                "connectto": (
                    '[{"target":"node_other","relation":"same_as",'
                    '"ref":"[SAME-AS demo.pdf/Other p2]","page":2}]'
                ),
                "page_nums": "1,2",
                "owned_page_nums": "1",
                "entities": '[{"text":"Acme","type":"organization"}]',
                "extra_metadata": {
                    "content_kind": "body",
                    "page_assets": [
                        {
                            "page_num": 1,
                            "artifact_ref": "page_citation_assets/page-1.png",
                            "content_type": "image/png",
                            "source": "knowhere-rendered-page-citation-source",
                        }
                    ],
                },
            }
        ]
    )
    package = build_parse_result_package(
        job_id="job-1",
        filename="demo.pdf",
        parse_output=ParseOutput(
            output_dir=str(tmp_path),
            chunks=canonical_chunks,
        ),
    )

    assert package.artifact.dataframe is None
    assert package.artifact.contents_count == 1
    canonical_metadata = package.chunks[0]["metadata"]

    zip_chunks = ZipChunkSchemaBuilder().format_chunks(package.chunks, {}, {})
    zip_metadata = zip_chunks[0]["metadata"]
    for key in (
        "summary",
        "entities",
        "keywords",
        "page_nums",
        "owned_page_nums",
        "connect_to",
        "page_assets",
        "content_kind",
    ):
        assert zip_metadata[key] == canonical_metadata[key]

    scope = DocumentPublicationScope(
        user_id="user-1",
        namespace="default",
        document_id="doc-1",
        job_result_id="result-1",
        source_file_name="demo.pdf",
    )
    section = DocumentSection(
        section_id="sec-1",
        user_id=scope.user_id,
        namespace=scope.namespace,
        document_id=scope.document_id,
        job_result_id=scope.job_result_id,
        section_path="demo.pdf / Section",
        section_title="Section",
        section_level=2,
        section_metadata={},
        sort_order=0,
    )
    stored = _build_document_chunk(
        chunk=package.chunks[0],
        chunk_metadata=_get_chunk_metadata(package.chunks[0]),
        source_path="demo.pdf/Section",
        section=section,
        scope=scope,
        fallback_sort_order=0,
    )

    assert stored.chunk_metadata == canonical_metadata
    assert stored.chunk_type == "page"
    assert stored.sort_order == 0
