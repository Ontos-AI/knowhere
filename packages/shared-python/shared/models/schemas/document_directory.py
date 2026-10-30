"""The initial, committed directory of a parsed document revision."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DocumentDirectorySection(BaseModel):
    model_config = ConfigDict(frozen=True)

    section_id: str
    parent_section_id: str | None
    section_path: str
    section_title: str | None
    section_level: int
    sort_order: int
    page_nums: list[int] = Field(default_factory=list)


class DocumentDirectoryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: str
    job_id: str
    job_result_id: str
    namespace: str
    directory_version: Literal[0, 1]
    directory_committed_at: str | None
    sections: list[DocumentDirectorySection] = Field(default_factory=list)
