"""Immutable background outputs, excluded from the live retrieval corpus."""

from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, JsonValue

from shared.services.storage.result_storage import ResultStorage


class StagedEnrichment(BaseModel):
    job_result_id: str
    scope_id: str
    claim_generation: str
    chunks: list[dict[str, JsonValue]] = Field(default_factory=list)
    section_summaries: dict[str, str] = Field(default_factory=dict)
    embeddings: dict[int, list[float]] = Field(default_factory=dict)

    def upload(
        self,
        *,
        storage: ResultStorage,
        job_id: str,
        raw_prefix: str | None,
        workspace_dir: str,
    ) -> str:
        reference: str = (
            f"staged/{self.job_result_id}/{self.scope_id}/{self.claim_generation}.json"
        )
        path: Path = Path(workspace_dir) / reference
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(), encoding="utf-8")
        storage.upload_raw_file(
            job_id=job_id,
            relative_path=reference,
            raw_prefix=raw_prefix,
            local_file_path=str(path),
        )
        return reference

    @classmethod
    def download(
        cls,
        *,
        storage: ResultStorage,
        job_id: str,
        raw_prefix: str | None,
        reference: str,
        workspace_dir: str,
        job_result_id: str,
        scope_id: str,
        claim_generation: str | None = None,
    ) -> Self:
        path: str = storage.download_raw_to_temp(
            job_id=job_id,
            relative_path=reference,
            raw_prefix=raw_prefix,
            suffix=".json",
            temp_dir=workspace_dir,
        )
        output: Self = cls.model_validate_json(Path(path).read_bytes())
        if (
            output.job_result_id != job_result_id
            or output.scope_id != scope_id
            or (
                claim_generation is not None
                and output.claim_generation != claim_generation
            )
        ):
            raise ValueError(
                "Staged enhancement does not belong to the committed claim"
            )
        return output
