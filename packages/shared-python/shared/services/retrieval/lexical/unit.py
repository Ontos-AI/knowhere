"""Prepared immutable lexical input; tokenization belongs to the publisher."""

from pydantic import BaseModel, ConfigDict, Field


class LexicalUnit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    documentId: str = Field(min_length=1)
    revisionId: str = Field(min_length=1)
    unitId: str = Field(min_length=1)
    sectionId: str = Field(min_length=1)
    pathTokens: str = ""
    contentTokens: str = ""
    sectionPath: str = ""
    sortOrder: int = 0
    hasImage: bool = False
    hasTable: bool = False
