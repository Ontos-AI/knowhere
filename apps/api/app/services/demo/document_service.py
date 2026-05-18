"""Demo Source catalog facade."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.demo.source_catalog import DemoSourceCatalog
from app.services.demo.source_materializer import (
    DemoSourceMaterializer,
    MaterializedDemoSource,
)
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.publication_service import RetrievalPublicationService


class DemoDocumentService:
    """Serves canonical demo data and delegates user-copy side effects."""

    def __init__(
        self,
        *,
        catalog: DemoSourceCatalog | None = None,
        publication_service: RetrievalPublicationService | None = None,
    ) -> None:
        self._catalog = catalog or DemoSourceCatalog()
        self._materializer = DemoSourceMaterializer(
            catalog=self._catalog,
            publication_service=publication_service,
        )

    def get_catalog(self) -> dict[str, Any]:
        return self._catalog.get_catalog()

    def list_chunks(
        self,
        *,
        demo_source_id: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any] | None:
        return self._catalog.list_chunks(
            demo_source_id=demo_source_id,
            page=page,
            page_size=page_size,
        )

    def get_chunk(
        self,
        *,
        demo_source_id: str,
        demo_chunk_id: str,
    ) -> dict[str, Any] | None:
        return self._catalog.get_chunk(
            demo_source_id=demo_source_id,
            demo_chunk_id=demo_chunk_id,
        )

    def get_original_file_path(self, *, demo_source_id: str) -> Path | None:
        return self._catalog.get_original_file_path(demo_source_id=demo_source_id)

    def get_asset_file_path(
        self,
        *,
        demo_source_id: str,
        asset_path: str,
    ) -> Path | None:
        return self._catalog.get_asset_file_path(
            demo_source_id=demo_source_id,
            asset_path=asset_path,
        )

    async def materialize_sources(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        demo_source_ids: list[str],
    ) -> list[MaterializedDemoSource]:
        return await self._materializer.materialize_sources(
            db,
            user_id=user_id,
            namespace=namespace,
            demo_source_ids=demo_source_ids,
        )
