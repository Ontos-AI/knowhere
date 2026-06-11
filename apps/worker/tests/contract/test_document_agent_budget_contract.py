from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.budget import BudgetTracker, StageEnvelope
from app.services.page_memory import memory_service
from shared.core.exceptions.domain_exceptions import ValidationException
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks

import pandas as pd
import pytest


def test_visual_stage_envelope_preserves_other_stage_guarantee() -> None:
    budget = BudgetTracker(
        plan_budget=100,
        visual_budget=100,
        visual_stage_envelopes={
            "toc_confirm": StageEnvelope(min_guarantee=30, cap=60),
            "coarse_planner": StageEnvelope(min_guarantee=40, cap=70),
        },
    )

    assert budget.try_reserve("visual", 30, stage="toc_confirm") is True
    budget.commit("visual", actual=25, est=30, stage="toc_confirm")
    assert budget.try_reserve("visual", 36, stage="toc_confirm") is False

    snapshot = budget.snapshot()
    assert snapshot["visual"]["used"] == 25
    assert snapshot["visual_stages"]["toc_confirm"]["used"] == 25

    assert budget.try_reserve("visual", 40, stage="coarse_planner") is True
    budget.refund("visual", est=40, stage="coarse_planner")
    assert budget.snapshot()["visual_stages"]["coarse_planner"]["reserved"] == 0


def test_visual_stage_cap_rejects_overage_while_legacy_calls_remain_supported() -> None:
    budget = BudgetTracker(
        plan_budget=100,
        visual_budget=100,
        visual_stage_envelopes={
            "toc_confirm": StageEnvelope(min_guarantee=0, cap=20),
        },
    )

    assert budget.try_reserve("visual", 21, stage="toc_confirm") is False
    assert budget.try_reserve("visual", 90) is True
    budget.commit("visual", actual=80, est=90)

    snapshot = budget.snapshot()
    assert snapshot["visual"]["used"] == 80
    assert snapshot["visual_stages"]["toc_confirm"]["used"] == 0


def test_dataframe_converter_accepts_page_chunks_with_extra_metadata() -> None:
    df = pd.DataFrame(
        [
            {
                "content": "[SUMMARY]\nshort\n\n[RAW]\nbody",
                "path": "demo.pdf/Root",
                "type": "page",
                "length": 26,
                "keywords": "",
                "summary": "short",
                "know_id": "page-1",
                "tokens": "",
                "connectto": "",
                "addtime": "2026-06-11 00:00:00",
                "page_nums": "1,2",
                "extra_metadata": {
                    "granularity": "whole_doc",
                    "page_image_uris": ["pages/page_page_1.png"],
                    "page_nums": [99],
                },
            }
        ]
    )

    chunks = dataframe_to_chunks(df)

    assert chunks[0]["type"] == "page"
    assert chunks[0]["metadata"]["granularity"] == "whole_doc"
    assert chunks[0]["metadata"]["page_nums"] == [1, 2]


def test_page_memory_unsupported_granularity_is_explicit() -> None:
    with pytest.raises(ValidationException) as exc_info:
        memory_service._raise_unsupported_granularity("page")  # noqa: SLF001

    error = exc_info.value
    assert "whole-document page memory" in error.user_message
    assert "PAGE_MEMORY_GRANULARITY_NOT_IMPLEMENTED" in error.internal_message
    assert error.violations[0]["field"] == "parse_track"
    assert "granularity=page" in error.violations[0]["description"]
