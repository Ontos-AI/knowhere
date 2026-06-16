"""Public entrypoint for document page anatomy profiling."""

from __future__ import annotations

import os
from typing import Any

from app.services.document_agent.coordinator import ProfileCoordinator
from app.services.document_agent.manifest import DocumentProfile, PageAnatomyMap


class ProfileAgent:
    def __init__(
        self,
        *,
        model: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self._model = model
        self._settings = settings or {}

    def run(
        self,
        file_path: str,
        job_id: str,
        *,
        output_dir: str | None = None,
        db: Any | None = None,
    ) -> PageAnatomyMap:
        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)
        coordinator = ProfileCoordinator(
            pdf_path=file_path,
            job_id=job_id,
            output_dir=output_dir,
            db=db,
            model=self._model,
            settings=self._settings,
        )
        return coordinator.run()

    def run_coarse(
        self,
        file_path: str,
        job_id: str,
        *,
        output_dir: str | None = None,
        db: Any | None = None,
    ) -> DocumentProfile:
        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)
        coordinator = ProfileCoordinator(
            pdf_path=file_path,
            job_id=job_id,
            output_dir=output_dir,
            db=db,
            model=self._model,
            settings=self._settings,
        )
        return coordinator.run_coarse()
