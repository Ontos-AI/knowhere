"""Pure tests for agent_tools section_path suffix resolution."""

from __future__ import annotations

from shared.services.retrieval.agent_tools.section_path_lookup import (
    format_ambiguous_section_path_error,
    paths_matching_section_ref,
)


def test_paths_matching_section_ref_exact() -> None:
    paths = ["附件目录 / 1.1 概述", "综合说明 / 1.1 概述"]
    assert paths_matching_section_ref("附件目录 / 1.1 概述", paths) == ["附件目录 / 1.1 概述"]


def test_paths_matching_section_ref_suffix() -> None:
    paths = [
        "附件目录 / 3 工程地质 / 3.2 覆盖层",
        "综合说明 / 3 工程地质 / 3.2 覆盖层",
    ]
    assert paths_matching_section_ref("3 工程地质 / 3.2 覆盖层", paths) == sorted(paths)


def test_paths_matching_section_ref_no_substring_false_positive() -> None:
    paths = ["附件目录 / 3.2 覆盖层处理", "附件目录 / 13.2 覆盖层"]
    assert paths_matching_section_ref("3.2 覆盖层", paths) == []


def test_paths_matching_section_ref_top_level() -> None:
    paths = ["附件目录", "附件目录 / 1.1 概述"]
    assert paths_matching_section_ref("附件目录", paths) == ["附件目录"]


def test_format_ambiguous_section_path_error_lists_candidates() -> None:
    matches = ["A / X", "B / X"]
    msg = format_ambiguous_section_path_error("X", matches)
    assert "ambiguous section_path 'X'" in msg
    assert "A / X" in msg
    assert "B / X" in msg
