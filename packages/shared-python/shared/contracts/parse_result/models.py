"""Parse-result ZIP v1. Unknown fields are additive; scalar coercion is disabled."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

SCHEMA_VERSION = 1
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonEmptyString = Annotated[str, Field(min_length=1)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, allow_inf_nan=False)


class VersionedArtifact(ContractModel):
    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


class Position(ContractModel):
    start: NonNegativeInt
    end: NonNegativeInt


class Connection(ContractModel):
    target: NonEmptyString
    relation: str
    ref: str | None = None
    position: Position | None = None
    score: float | None = None
    keywords: list[str] | None = None
    same_as_owner: str | None = None


class PageAsset(ContractModel):
    page_num: PositiveInt
    artifact_ref: NonEmptyString
    content_type: NonEmptyString
    source: NonEmptyString
    asset_url: str | None = None
    width: PositiveInt | None = None
    height: PositiveInt | None = None


class ChunkMetadata(ContractModel):
    length: NonNegativeInt
    summary: str
    page_nums: list[int]
    # Existing text writers support both token count and pretokenized terms.
    tokens: NonNegativeInt | list[str] | None = None
    keywords: list[str] | None = None
    connect_to: list[str | Connection] | None = None
    file_path: str | None = None
    page_assets: list[PageAsset] | None = None


class ChunkRecord(ContractModel):
    chunk_id: NonEmptyString
    type: Literal["text", "image", "table", "page"]
    content: str
    path: str
    metadata: ChunkMetadata


class Chunks(VersionedArtifact):
    chunks: list[ChunkRecord]


class ChunkStatistics(ContractModel):
    total_chunks: NonNegativeInt
    text_chunks: NonNegativeInt
    image_chunks: NonNegativeInt
    table_chunks: NonNegativeInt
    # Older enriched navigation files predate page chunks.
    page_chunks: NonNegativeInt = 0


class ManifestStatistics(ChunkStatistics):
    total_pages: NonNegativeInt | None = None


class NavigationStatistics(ChunkStatistics):
    max_depth: NonNegativeInt


class Section(ContractModel):
    title: str
    path: str
    # Existing enriched navigation may omit level; nesting defines depth.
    level: PositiveInt | None = None
    summary: str
    chunk_count: NonNegativeInt
    children: list[Section]


class Resource(ContractModel):
    path: str
    summary: str


class Resources(ContractModel):
    images: list[Resource]
    tables: list[Resource]


class DocNav(VersionedArtifact):
    version: str
    file_name: str
    stats: NavigationStatistics
    sections: list[Section]
    resources: Resources


class ProcessingCost(ContractModel):
    micro_dollars: int | None
    credits: float | None


class ProcessingTiming(ContractModel):
    started_at: str | None
    completed_at: str | None
    duration_ms: int | None


class Processing(ContractModel):
    page_count: int | None
    billing_status: str | None
    cost: ProcessingCost
    timing: ProcessingTiming
    stages: dict[str, Any]


class Hierarchy(RootModel[dict[str, "Hierarchy"]]):
    model_config = ConfigDict(strict=True)


class Manifest(VersionedArtifact):
    version: str
    job_id: NonEmptyString
    data_id: str | None
    source_file_name: str
    processing_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T.+Z$")]
    processing: Processing
    statistics: ManifestStatistics
    HIERARCHY: Hierarchy
