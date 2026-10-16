"""Contract tests for the document domain taxonomy and its prompt rendering."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from app.services.document_agent.coarse_profile.prompts import (
    COARSE_PROFILE_INSTRUCTIONS,
)
from app.services.document_parser.profiling.domain_taxonomy import (
    DOMAIN_MENU,
    DOMAIN_SECTIONS,
    OTHER_CODE,
    domain_codes_from_ids,
)

_LEAVES = [leaf for section in DOMAIN_SECTIONS for leaf in section.leaves]


def test_leaf_codes_are_unique_iptc_or_other() -> None:
    codes = [leaf.code for leaf in _LEAVES]
    assert len(codes) == len(set(codes))
    assert all(
        code == OTHER_CODE or (code.isdigit() and len(code) == 8) for code in codes
    )
    assert len(_LEAVES) == 18  # 17 IPTC top-level + OTHER


def test_every_section_has_leaves_and_other_is_last() -> None:
    assert all(section.leaves for section in DOMAIN_SECTIONS)
    assert _LEAVES[-1].code == OTHER_CODE
    assert [leaf.code for leaf in _LEAVES].count(OTHER_CODE) == 1


def test_menu_numbers_leaves_contiguously_and_leaves_sections_unnumbered() -> None:
    numbered = [line for line in DOMAIN_MENU.splitlines() if line[:1].isdigit()]
    assert len(numbered) == len(_LEAVES)
    for expected, line in enumerate(numbered, start=1):
        assert line.startswith(f"{expected}. ")
    headers = [line for line in DOMAIN_MENU.splitlines() if not line[:1].isdigit()]
    assert headers == [section.title for section in DOMAIN_SECTIONS]


def test_ids_map_to_menu_order_codes() -> None:
    assert domain_codes_from_ids([1]) == [_LEAVES[0].code]
    assert domain_codes_from_ids([len(_LEAVES)]) == [OTHER_CODE]
    assert domain_codes_from_ids([3, 1]) == [_LEAVES[2].code, _LEAVES[0].code]


@pytest.mark.parametrize(
    "ids",
    [None, [], "1", [0], [10_000], ["1"], [True], [1, 1], [len(_LEAVES), 1]],
)
def test_invalid_ids_raise(ids) -> None:
    with pytest.raises(ValueError):
        domain_codes_from_ids(ids)


def test_coarse_prompt_carries_menu_and_json_word() -> None:
    assert DOMAIN_MENU in COARSE_PROFILE_INSTRUCTIONS
    assert "json" in COARSE_PROFILE_INSTRUCTIONS.lower()
