from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class QueryMode(str, Enum):
    AGENT = "agent"
    CLASSIC = "classic"


@dataclass(frozen=True)
class RouterDecision:
    mode: QueryMode
    reason: str
    source: str
    model_name: str
    tokens_used: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "reason": self.reason,
            "source": self.source,
            "model_name": self.model_name,
            "tokens_used": self.tokens_used,
        }
