"""Authorization-qualified identity of a lexical namespace."""

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LexicalScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ownerId: str = Field(min_length=1)
    namespace: str = Field(min_length=1, max_length=255)
    corpusKind: Literal["private", "demo"] = "private"

    @property
    def fingerprint(self) -> str:
        serialized: str = json.dumps(
            [self.corpusKind, self.ownerId, self.namespace],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return sha256(serialized.encode()).hexdigest()
