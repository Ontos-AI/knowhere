"""Minimal stand-ins for experiment-repo ``agent_delivery`` symbols.

Production map-nav runs with ``toolspace=ProviderToolSpace(...)`` and
``compose_answer=False``, so most of these are type/shape only. Dense fuse is
hard-off until shared with three-channel vector wiring
(``map_dense_enabled`` returns False; dense helpers stay in place).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Chunk:
    node_id: str
    doc_id: str
    text: str
    line_ids: Tuple[int, ...]
    section_id: Optional[str] = None
    text_line_id_groups: Optional[Tuple[Tuple[int, ...], ...]] = None


@dataclass
class AgentStep:
    step_idx: int
    action: str
    detail: Dict[str, object] = field(default_factory=dict)


@dataclass
class EpisodeResult:
    representation: str
    steps: List[AgentStep]
    scored_chunks: List[Tuple[Chunk, float]]
    kept_chunks: List[Chunk]
    evidence_text: str
    evidence_chars_actual: int
    retrieved_nodes: List[str]
    composed_answer: str = ""
    section_ids: List[str] = field(default_factory=list)
    trajectory_length: int = 0
    truncated_last: bool = False
    refusal_events: List[Dict[str, object]] = field(default_factory=list)
    phase_timings: Dict[str, float] = field(default_factory=dict)
    stop_reason: str = "completed"


# Duck-typed ToolSpace is provided by ``ProviderToolSpace``; keep a type alias.
ToolSpace = Any
HierarchicalTools = Any


class Refusal(Exception):
    """Raised by experimental ToolSpace; unused on the ProviderToolSpace path."""


def line_node_id(doc_id: str, line_id: int) -> str:
    """Experiment-corpus helper; ProviderToolSpace never hits this path."""
    raise NotImplementedError(
        "line_node_id is not available in Knowhere nav; use ProviderToolSpace"
    )


def compose_answer_llm(*_args: Any, **_kwargs: Any) -> str:
    raise NotImplementedError(
        "compose_answer_llm is not wired in Knowhere retrieval; pass compose_answer=False"
    )


def snapshot_usage() -> Dict[str, Any]:
    """Fallback when no ``nav_token_episode`` is active."""
    return {}


def record_usage(*_args: Any, **_kwargs: Any) -> None:
    return None


def load_llm_env() -> None:
    return None


def require_llm_env(*_args: Any, **_kwargs: Any) -> None:
    return None


def make_openai_client(*_args: Any, **_kwargs: Any) -> Any:
    raise NotImplementedError("Install set_nav_chat_backend(...) for Knowhere nav LLM")


def resolve_thinking_mode(raw: Optional[str]) -> str:
    text = str(raw or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled", "enable"}:
        return "enabled"
    if text in {"0", "false", "no", "off", "disabled", "disable"}:
        return "disabled"
    return "disabled"


def chat_thinking_extra(*, mode: str, model: str = "") -> Dict[str, Any]:
    del model
    typ = "enabled" if mode == "enabled" else "disabled"
    return {"extra_body": {"thinking": {"type": typ}}}


def resolve_chat_credentials(*_args: Any, **_kwargs: Any) -> Tuple[str, str]:
    return "", ""


def cached_chat_completion(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
    raise NotImplementedError(
        "Install set_nav_chat_backend(...) before calling nav_chat in Knowhere"
    )
