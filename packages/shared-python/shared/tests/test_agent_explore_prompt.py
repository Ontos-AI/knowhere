"""Loop rules and model-visible jargon scan."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.retrieval.agent_explore.config import (
    FINISH_TOOL_DESCRIPTION,
    FINISH_TOOL_SCHEMA,
    PICK_TOOL_DESCRIPTION,
    PICK_TOOL_NAME,
    PICK_TOOL_SCHEMA,
)
from shared.services.retrieval.agent_explore.prompt import AGENT_SYSTEM_PROMPT, LOOP_RULES
from shared.services.retrieval.agent_explore.shared import validate_pick_args
from shared.services.retrieval.agent_tools import REGISTRY

_BANNED = (
    "term_search_text",
    "SAME-AS",
    "connect_to",
    "Root",
    "page-track",
    "parse_track",
    "page_assets",
    "resolve_same_as",
)


def test_loop_rules_state_round_read_and_pick_contract() -> None:
    assert "On your first\n  turn you have no results yet, so do not call corpus.read." in LOOP_RULES
    assert "Calls in the same turn cannot use each other's results." in LOOP_RULES
    assert "the next turn is\n  a pick phase: you can only call corpus.pick." in LOOP_RULES
    assert "Body text you read but did not pick cannot be read again." in LOOP_RULES
    assert "the steps used so far" in LOOP_RULES
    for removed in ("expire", "to pick later", '"pick" field', "remaining budget"):
        assert removed not in LOOP_RULES, removed


def test_pick_is_its_own_tool_and_absent_from_finish_and_registry() -> None:
    assert REGISTRY.get(PICK_TOOL_NAME) is None
    assert "pick" not in FINISH_TOOL_SCHEMA["properties"]  # type: ignore[operator]
    assert "pick" not in FINISH_TOOL_DESCRIPTION
    assert PICK_TOOL_SCHEMA["required"] == ["pick"]
    assert "cannot be read again" in PICK_TOOL_DESCRIPTION
    for spec in REGISTRY.all():
        assert "pick" not in (spec.json_schema.get("properties") or {}), spec.name


def test_pick_args_follow_pick_tool_schema() -> None:
    assert validate_pick_args({"pick": []}) is None
    assert validate_pick_args({"pick": ["R1.1", "O2"]}) is None
    assert validate_pick_args({"pick_ids": ["R1.1"]}) is not None
    assert validate_pick_args({}) is not None
    assert validate_pick_args({"pick": ["x"]}) is not None


def _schema_descriptions(schema: object) -> list[str]:
    texts: list[str] = []
    if not isinstance(schema, dict):
        return texts
    description = schema.get("description")
    if isinstance(description, str):
        texts.append(description)
    for child in (schema.get("properties") or {}).values():
        texts.extend(_schema_descriptions(child))
    if "items" in schema:
        texts.extend(_schema_descriptions(schema["items"]))
    for key in ("anyOf", "oneOf", "allOf"):
        for child in schema.get(key) or []:
            texts.extend(_schema_descriptions(child))
    return texts


def test_model_visible_text_has_no_internal_jargon() -> None:
    surfaces = [AGENT_SYSTEM_PROMPT, PICK_TOOL_DESCRIPTION]
    surfaces.extend(_schema_descriptions(PICK_TOOL_SCHEMA))
    for spec in REGISTRY.all():
        surfaces.append(spec.description)
        surfaces.extend(_schema_descriptions(spec.json_schema))
    blob = "\n".join(surfaces)
    for banned in _BANNED:
        assert banned not in blob, banned
