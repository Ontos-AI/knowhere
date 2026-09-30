"""Pick-field wrapper and model-visible jargon scan."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.retrieval.agent_explore.prompt import (
    AGENT_SYSTEM_PROMPT,
    PICK_FIELD_SCHEMA,
    split_pick,
    with_pick_field,
)
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


def test_with_pick_field_does_not_mutate_original_schema() -> None:
    original = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "q"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    snapshot = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "q"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    wrapped = with_pick_field(original)
    assert original == snapshot
    assert wrapped["properties"]["query"] == original["properties"]["query"]
    assert wrapped["properties"]["pick"] == PICK_FIELD_SCHEMA
    assert "pick" not in original["properties"]


def test_split_pick_strips_and_ignores_non_list() -> None:
    handles, rest = split_pick({"pick": [" R1.1 ", "", "O2"], "refs": [1]})
    assert handles == ["R1.1", "O2"]
    assert rest == {"refs": [1]}
    assert split_pick({"notes": "x"}) == ([], {"notes": "x"})
    assert split_pick({"pick": "R1.1"}) == ([], {})


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
    surfaces = [AGENT_SYSTEM_PROMPT]
    for spec in REGISTRY.all():
        surfaces.append(spec.description)
        surfaces.extend(_schema_descriptions(spec.json_schema))
    blob = "\n".join(surfaces)
    for banned in _BANNED:
        assert banned not in blob, banned
