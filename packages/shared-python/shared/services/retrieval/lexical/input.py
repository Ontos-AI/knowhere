"""Checksummed immutable indexing input retained for eligible job retries."""

from pydantic import BaseModel, ConfigDict

from shared.services.retrieval.lexical.unit import LexicalUnit


class LexicalInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    formatVersion: int = 1
    compilerFingerprint: str
    units: tuple[LexicalUnit, ...]
