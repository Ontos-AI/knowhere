"""Retrieval LLM calls must carry an explicit usage_task."""

from __future__ import annotations

import ast
from pathlib import Path

_RETRIEVAL_ROOT = (
    Path(__file__).resolve().parents[1] / "services" / "retrieval"
)
_LLM_CALL_NAMES = {
    "chat_completion",
    "chat_completion_with_usage",
    "chat_completion_raw_with_usage",
    "embed_query",
    "embed_images",
}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_retrieval_llm_calls_pass_explicit_usage_task() -> None:
    missing: list[str] = []
    for path in _RETRIEVAL_ROOT.rglob("*.py"):
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in _LLM_CALL_NAMES:
                continue
            if any(keyword.arg == "usage_task" for keyword in node.keywords):
                continue
            missing.append(f"{path.relative_to(_RETRIEVAL_ROOT)}:{node.lineno}:{name}")
    assert missing == []
