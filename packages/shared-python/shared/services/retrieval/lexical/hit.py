"""A lexical result bound to its physical build and immutable revision."""

from pydantic import BaseModel, ConfigDict

from shared.services.retrieval.lexical.unit import LexicalUnit


class LexicalHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    unit: LexicalUnit
    buildId: str
    score: float
    pathRank: int | None = None
    contentRank: int | None = None
