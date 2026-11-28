from __future__ import annotations

import sys
from types import SimpleNamespace

import gevent
import pandas as pd
import pytest

from app.services.document_parser.structure import markdown_heading_preparation
from app.services.document_parser.structure.heading_candidates import filter_markdown_headings
from app.services.document_parser.structure.heading_hierarchy import (
    HeadingHierarchyInput,
    predict_heading_hierarchy,
)
from shared.core.exceptions.domain_exceptions import TimeoutException


def test_markdown_heading_prediction_yields_to_the_worker_loop() -> None:
    heartbeat: gevent.Greenlet = gevent.spawn(lambda: None)
    try:
        predictions: pd.DataFrame = predict_heading_hierarchy(
            HeadingHierarchyInput(infos=["# Heading", "Body text"], doc_type="md")
        )

        assert heartbeat.ready()
        assert predictions["level"].tolist() == [1, -1]
    finally:
        heartbeat.kill()


def test_markdown_heading_prediction_keeps_pre_toc_reasons() -> None:
    lines: list[str] = ["# Cover.", "Front matter", "# Heading", "# A sentence."]
    initial_predictions: pd.DataFrame = filter_markdown_headings(lines)

    predictions: pd.DataFrame = predict_heading_hierarchy(
        HeadingHierarchyInput(
            infos=lines,
            doc_type="md",
            toc_hierarchies=[{"toc_range": [2, 2]}],
        )
    )

    assert predictions["id"].tolist() == [0, 1, 2, 3]
    assert predictions["level"].tolist() == [-1, -1, 1, -1]
    assert (
        predictions["reason"].iloc[:2].tolist()
        == initial_predictions["reason"].iloc[:2].tolist()
    )
    assert predictions["reason"].iloc[3] != initial_predictions["reason"].iloc[3]


def test_markdown_heading_relocation_uses_first_prediction_for_each_line() -> None:
    from app.services.document_parser.formats.markdown.parser import heading_md_relocate

    lines: list[str] = ["# First", "# Keep the original text", "# Body", " "]
    predictions: pd.DataFrame = pd.DataFrame(
        [(0, 2, "First continued"), (0, 3, "Duplicate"), (1, 1, "")],
        columns=["id", "level", "heading"],
    )

    assert heading_md_relocate(lines, predictions) == [
        "## First continued",
        "# Keep the original text",
        "Body",
    ]


def test_markdown_heading_timeout_reaps_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess = markdown_heading_preparation.subprocess
    start_process = subprocess.Popen
    children: list[subprocess.Popen] = []

    def start_sleeping_child(command: list[str], **kwargs: object) -> subprocess.Popen:
        process = start_process([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", start_sleeping_child)
    monkeypatch.setattr(
        markdown_heading_preparation,
        "settings",
        SimpleNamespace(TASK_TIME_LIMIT_SECONDS=0.1, DOCUMENT_INGESTION_TASK_RETRY_COUNTDOWN=120),
    )

    with pytest.raises(TimeoutException):
        markdown_heading_preparation.prepare_markdown_headings(["# Heading"], None)

    assert len(children) == 1 and children[0].poll() is not None
