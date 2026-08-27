"""Parse-pipeline LLM calls must carry an explicit usage_task."""

from __future__ import annotations

import ast
from pathlib import Path

_WORKER_SERVICES = Path(__file__).resolve().parents[2] / "app" / "services"
_WORKER_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_PARSE_ROOTS = (
    _WORKER_SERVICES / "document_parser",
    _WORKER_SERVICES / "document_agent",
    _WORKER_SERVICES / "page_memory",
    _WORKER_SERVICES / "connect_builder",
    _WORKER_SCRIPTS,
)
_LLM_CALL_NAMES = {
    "chat_completion",
    "chat_completion_with_usage",
    "chat_completion_raw_with_usage",
    "summarize",
    "transcribe",
}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _iter_parse_python_files() -> list[Path]:
    files: list[Path] = []
    for root in _PARSE_ROOTS:
        if not root.exists():
            continue
        files.extend(path for path in root.rglob("*.py") if path.is_file())
    return files


def _relative_path(path: Path) -> str:
    for anchor in (_WORKER_SERVICES, _WORKER_SCRIPTS):
        try:
            return str(path.relative_to(anchor))
        except ValueError:
            continue
    return str(path)


def test_parse_llm_calls_pass_explicit_usage_task() -> None:
    missing: list[str] = []
    for path in _iter_parse_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in _LLM_CALL_NAMES:
                continue
            if any(keyword.arg == "usage_task" for keyword in node.keywords):
                continue
            rel = _relative_path(path)
            missing.append(f"{rel}:{node.lineno}:{name}")
    assert missing == []
