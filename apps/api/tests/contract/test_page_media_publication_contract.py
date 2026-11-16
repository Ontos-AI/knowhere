"""Embedded media survives republishing: page fields, media chunks, and their hosts."""

from __future__ import annotations

import asyncio

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job import Job
from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
from shared.services.retrieval.agent_tools.registry import ToolContext
from shared.services.retrieval.agent_tools.tools.assets import assets
from shared.services.retrieval.agent_tools.tools.grep import grep
from shared.services.retrieval.lexical.worker import IndexingWorker
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.publication_scope_content import (
    replace_document_scope_content,
)
from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)
from tests.support.publication_benchmark_support import ensure_benchmark_import_path

ensure_benchmark_import_path()

from scripts.publication_benchmark.publication_execution import (  # noqa: E402
    PublicationScope,
    seed_publication_fixture,
)

_VIDEO = {
    "asset_id": "asset_demo_video",
    "kind": "video",
    "origin": "embedded",
    "title": "Product demo",
    "summary": None,
    "page_nums": [2],
    "region": {"page_num": 2, "bbox": [0.1, 0.2, 0.6, 0.8], "coord_system": "page_fraction"},
    "content_type": "video/mp4",
    "size_bytes": 1024,
    "checksum_sha256": "a" * 64,
    "duration_ms": 53_700,
    "ref": {"kind": "artifact", "path": "embedded_media/aaaaaaaaaaaa-media1.mp4"},
}
_MARKER = "[video: Product demo · 00:53 · asset:asset_demo_video]"
_VIDEO_CHUNK = {
    "chunk_id": "asset_demo_video",
    "type": "video",
    "content": f"{_MARKER}\n\n[00:00:00–00:00:04] S1: Welcome to the launch",
    "path": "embedded_media/asset_demo_video",
    "metadata": {
        "page_nums": [2],
        "asset_title": "Product demo",
        "asset": _VIDEO,
        "connect_to": [{"target": "media-m0-p2", "relation": "embedded_by"}],
    },
}


def _m0_page(page: int, *, content: str, media: bool) -> dict[str, object]:
    metadata: dict[str, object] = {
        "scope_id": f"p{page}-{page}",
        "enrichment_state": "text",
        "page_nums": [page],
    }
    if media:
        metadata["assets"] = [dict(_VIDEO)]
        metadata["hidden_page_nums"] = [page]
        metadata["connect_to"] = [{"target": _VIDEO["asset_id"], "relation": "embeds"}]
    return {
        "chunk_id": f"media-m0-p{page}",
        "type": "page",
        "content": content,
        "path": f"page-media.pptx/p{page}",
        "metadata": metadata,
    }


def test_coarse_republish_keeps_page_media_and_assets_lists_it(
    monkeypatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    # The async engine reads the contract database settings when first imported.
    from shared.core.database import get_db_context

    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    scope = PublicationScope(
        user_ref="page-media-user",
        namespace_ref="page-media",
        source_file_name="page-media.pptx",
        job_ref="page-media-job",
        revision_ref="page-media-result",
    )
    try:
        with Session(engine) as session:
            seed_publication_fixture(session, scope=scope)
            job = session.execute(select(Job).where(Job.job_id == scope.job_ref)).scalar_one()
            job.job_metadata = {
                **dict(job.job_metadata or {}),
                "parse_track": "page_memory",
                "ingestion_mode": "progressive",
                "enrichment_status": "text",
            }
            session.commit()
            chunks = [
                _m0_page(1, content="Welcome", media=False),
                _m0_page(2, content=f"Roadmap\n{_MARKER}", media=True),
                _VIDEO_CHUNK,
            ]
            finalization = SyncJobSuccessFinalizer().finalize(
                session,
                job_id=scope.job_ref,
                result_s3_key="results/page-media/result.zip",
                checksum="a" * 64,
                zip_size=1,
                chunks=chunks,
                stored_count=len(chunks),
                delivery_mode="url",
                section_summaries=None,
            )
            assert finalization.response.should_commit()
            session.commit()
            worker = IndexingWorker(sessionFactory=sessionmaker(session.get_bind()))
            assert worker.run(scope.job_ref, initialOnly=True) == "completed"
            document_id = str(
                session.execute(
                    select(Document.document_id).where(
                        Document.current_job_result_id == scope.revision_ref
                    )
                ).scalar_one()
            )

        with Session(engine) as session:
            replace_document_scope_content(
                session,
                scope=DocumentPublicationScope(
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    document_id=document_id,
                    job_result_id=scope.revision_ref,
                    source_file_name=scope.source_file_name,
                ),
                scope_id="*",
                chunks=[
                    {
                        "chunk_id": "coarse-intro",
                        "type": "page",
                        "content": "Welcome",
                        "path": "page-media.pptx/Intro",
                        "metadata": {"scope_id": "p1-1", "enrichment_state": "coarse", "page_nums": [1]},
                    },
                    {
                        "chunk_id": "coarse-plan",
                        "type": "page",
                        "content": "Roadmap and launch plan",
                        "path": "page-media.pptx/Plan",
                        "metadata": {"scope_id": "p2-3", "enrichment_state": "coarse", "page_nums": [2, 3]},
                    },
                ],
            )
            session.commit()

        with Session(engine) as session:
            by_id = {
                chunk.chunk_id: chunk
                for chunk in session.scalars(
                    select(DocumentChunk).where(DocumentChunk.document_id == document_id)
                ).all()
            }
            assert set(by_id) == {"coarse-intro", "coarse-plan", "asset_demo_video"}
            intro, plan = by_id["coarse-intro"], by_id["coarse-plan"]
            video = by_id["asset_demo_video"]
            assert video.chunk_type == "video"
            assert video.content == _VIDEO_CHUNK["content"]
            assert (video.chunk_metadata or {})["asset"] == _VIDEO
            assert (video.chunk_metadata or {})["connect_to"] == [
                {"target": "coarse-plan", "relation": "embedded_by"}
            ]
            assert (plan.chunk_metadata or {})["connect_to"] == [
                {"target": "asset_demo_video", "relation": "embeds"}
            ]
            plan_section_path = session.execute(
                select(DocumentSection.section_path).where(
                    DocumentSection.section_id == plan.section_id
                )
            ).scalar_one()
            assert "assets" not in (intro.chunk_metadata or {})
            assert intro.content == "Welcome"
            assert plan.content == f"Roadmap and launch plan\n{_MARKER}"
            assert (plan.chunk_metadata or {})["assets"] == [_VIDEO]
            assert (plan.chunk_metadata or {})["hidden_page_nums"] == [2]
            assert (plan.chunk_metadata or {})["length"] == len(plan.content or "")

        with Session(engine) as session:
            replace_document_scope_content(
                session,
                scope=DocumentPublicationScope(
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    document_id=document_id,
                    job_result_id=scope.revision_ref,
                    source_file_name=scope.source_file_name,
                ),
                scope_id="p2-3",
                chunks=[
                    {
                        "chunk_id": "refined-plan",
                        "type": "page",
                        "content": "Launch roadmap",
                        "path": "page-media.pptx/Plan/Launch",
                        "metadata": {"page_nums": [2, 3]},
                    },
                ],
            )
            session.commit()

        with Session(engine) as session:
            refined = {
                chunk.chunk_id: chunk
                for chunk in session.scalars(
                    select(DocumentChunk).where(DocumentChunk.document_id == document_id)
                ).all()
            }
            assert set(refined) == {"coarse-intro", "refined-plan", "asset_demo_video"}
            assert (refined["refined-plan"].chunk_metadata or {})["connect_to"] == [
                {"target": "asset_demo_video", "relation": "embeds"}
            ]
            assert refined["refined-plan"].content == f"Launch roadmap\n{_MARKER}"
            assert (refined["asset_demo_video"].chunk_metadata or {})["connect_to"] == [
                {"target": "refined-plan", "relation": "embedded_by"}
            ]
            plan_section_path = session.execute(
                select(DocumentSection.section_path).where(
                    DocumentSection.section_id == refined["refined-plan"].section_id
                )
            ).scalar_one()

        async def _list_assets(asset_type: str) -> list[dict[str, object]]:
            async with get_db_context() as db:
                result = await assets(
                    ToolContext(
                        db=db,
                        user_id=scope.user_ref,
                        namespace=scope.namespace_ref,
                        db_factory=get_db_context,
                    ),
                    {"type": asset_type},
                )
                assert result.error is None
                return result.payload["rows"]

        async def _grep(pattern: str) -> list[dict[str, object]]:
            async with get_db_context() as db:
                result = await grep(
                    ToolContext(
                        db=db,
                        user_id=scope.user_ref,
                        namespace=scope.namespace_ref,
                        db_factory=get_db_context,
                    ),
                    {"pattern": pattern},
                )
                assert result.error is None
                return result.payload["rows"]

        transcript_hits = asyncio.run(_grep("Welcome to the launch"))
        assert [(row["kind"], row["chunk_id"]) for row in transcript_hits] == [
            ("video", "asset_demo_video")
        ]
        assert transcript_hits[0]["section_path"] == plan_section_path

        video_rows = asyncio.run(_list_assets("video"))
        assert [(row["kind"], row["chunk_id"]) for row in video_rows] == [
            ("video", "asset_demo_video")
        ]
        assert video_rows[0]["asset"] == _VIDEO
        assert video_rows[0]["summary"] == _MARKER
        assert video_rows[0]["hosted"] is True
        assert video_rows[0]["section_path"] == plan_section_path
        assert asyncio.run(_list_assets("audio")) == []
        assert asyncio.run(_list_assets("image")) == []
    finally:
        engine.dispose()
