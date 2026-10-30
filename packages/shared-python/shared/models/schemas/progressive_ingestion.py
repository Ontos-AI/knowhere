"""Durable enhancement state and immutable reader artifact identities."""

from typing import Annotated, Literal, Self
from typing_extensions import TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnhancementProgress(BaseModel):
    status: Literal["pending", "running", "complete", "failed"] = "pending"
    can_retry: bool = False
    attempt: int = Field(default=1, ge=1)
    error: str | None = None

    @model_validator(mode="after")
    def validate_retry_eligibility(self) -> Self:
        if self.can_retry and self.status != "failed":
            raise ValueError("Only a terminal enhancement failure can be retried")
        return self


class StoredSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: str = Field(min_length=1)
    stage: Literal["baseline", "final"]
    job_id: str = Field(min_length=1)
    job_result_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_key: str = Field(min_length=1)


class SnapshotObjectAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[Annotated[str, Field(strict=True, min_length=1, max_length=2048)]] = (
        Field(min_length=1, max_length=200)
    )

    @model_validator(mode="after")
    def validate_unique_paths(self) -> Self:
        if len(set(self.paths)) != len(self.paths):
            raise ValueError("Snapshot object paths must be unique")
        return self


class EnhancementRetryResponse(BaseModel):
    job_id: str
    enhancement: EnhancementProgress


class PublishedSnapshot(BaseModel):
    snapshot_id: str
    stage: Literal["baseline", "final"]
    job_id: str
    job_result_id: str
    document_id: str
    checksum: str
    result_url: str
    result_url_expires_at: str


class EnhancementStage(TypedDict, total=False):
    status: Literal["pending", "running", "complete", "failed"]
    attempts: int
    claim_generation: str | None
    claimed_at: str | None
    error: str | None
    pending_pages: list[int]
    next_retry_at: str | None


class ProgressiveIngestionState(BaseModel):
    # Stage claims remain internal. Extra fields preserve them across public
    # projection/retry updates without exposing them as client configuration.
    model_config = ConfigDict(extra="allow")

    enhancement: EnhancementProgress = Field(default_factory=EnhancementProgress)
    snapshots: list[StoredSnapshot] = Field(default_factory=list, max_length=2)
    current_snapshot_id: str | None = None
    stages: dict[str, EnhancementStage] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_snapshot_identity(self) -> Self:
        identities: set[str] = {snapshot.snapshot_id for snapshot in self.snapshots}
        stages: set[str] = {snapshot.stage for snapshot in self.snapshots}
        if len(identities) != len(self.snapshots) or len(stages) != len(self.snapshots):
            raise ValueError("Each reader snapshot and stage must be unique")
        if (
            self.current_snapshot_id is not None
            and self.current_snapshot_id not in identities
        ):
            raise ValueError("Current snapshot must reference a committed artifact")
        return self
