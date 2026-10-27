"""Protocol tests: coarse profile parse."""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.coarse_profile.classifier import _parse_profile
from app.services.document_parser.profiling.domain_taxonomy import (
    DOMAIN_SECTIONS,
    OTHER_CODE,
)


def _menu_id(code: str) -> int:
    return next(
        number
        for number, leaf_code in enumerate(
            (leaf.code for section in DOMAIN_SECTIONS for leaf in section.leaves),
            start=1,
        )
        if leaf_code == code
    )


def test_parse_profile_reads_classification_fields() -> None:
    raw = json.dumps(
        {
            "is_scanned": True,
            "domain_ids": [_menu_id("04000000"), _menu_id("13000000")],
            "category_label": "Feasibility Study Report",
            "routing_category": "generic",
            "language": "zh",
            "header_y": None,
            "footer_y": None,
        }
    )
    profile = _parse_profile(raw)
    assert profile.is_scanned is True
    assert profile.category == ["04000000", "13000000"]
    assert profile.category_label == "Feasibility Study Report"
    assert profile.routing_category == "generic"
    assert profile.language == "zh"


@pytest.mark.parametrize(
    "domain_ids",
    [
        None,
        [],
        "04000000",
        [0],
        [10_000],
        ["5"],
        [True],
        [1, 1],
        [_menu_id(OTHER_CODE), 1],
    ],
)
def test_parse_profile_falls_back_to_other_for_invalid_domain_ids(domain_ids) -> None:
    raw = json.dumps(
        {
            "is_scanned": False,
            "domain_ids": domain_ids,
            "category_label": "Report",
            "routing_category": "generic",
            "language": "en",
        }
    )
    profile = _parse_profile(raw)
    assert profile.category == [OTHER_CODE]
    assert profile.category_label == "Report"


def test_parse_profile_does_not_route_from_category_label() -> None:
    raw = json.dumps(
        {
            "is_scanned": False,
            "domain_ids": [_menu_id(OTHER_CODE)],
            "category_label": "Engineering Atlas",
            "language": "en",
        }
    )
    assert _parse_profile(raw).routing_category == "generic"


def test_parse_profile_rejects_invalid_header_footer_order() -> None:
    raw = json.dumps(
        {
            "is_scanned": False,
            "domain_ids": [_menu_id(OTHER_CODE)],
            "category_label": "Report",
            "routing_category": "generic",
            "language": "en",
            "header_y": 0.8,
            "footer_y": 0.2,
        }
    )
    profile = _parse_profile(raw)
    assert profile.header_y is None
    assert profile.footer_y is None
