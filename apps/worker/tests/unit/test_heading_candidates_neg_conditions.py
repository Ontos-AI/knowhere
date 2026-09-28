"""NEG punctuation rule in heading candidate detection.

``remove_by_conditions`` treats ``。`` / ``！`` / ``；`` as body text whether
they sit mid-line or at the end of the line. Production MD/PDF scoring uses
``_estimate_markdown_heading_level`` → ``remove_by_conditions``.
"""

from __future__ import annotations

from app.services.document_parser.structure.heading_candidates import (
    _estimate_markdown_heading_level,
    remove_by_conditions,
)

# Exact lines from job_05570e6e9fb5 full.md (chapter 3). Production ids
# 23 / 29 / 30 / 36 / 41 are the filter_markdown_headings row ids for these
# lines; the texts below are the live input to the NEG rule.
_REAL_3_CONSENSUS = "## 3 共识要点"
_REAL_321_SCREENING_TARGET = (
    "3.2.1 筛查对象 对所有意识清楚能合作的住院冠心病患者在入院 24 h "
    "内筛查其是否存在焦虑、抑郁情绪。"
)
_REAL_322_SCREENING_TOOLS = "## 3.2.2 筛查工具"
_REAL_331_ASSESSMENT_TARGET = (
    "3.3.1 评估对象 患者 PHQ-2 得分≥2 分, 则需进行进一步评估; "
    "GAD-2 得分≥2 分, 则需进行进一步评估。"
)
_REAL_35_INTERVENTION = "## 3.5 心理护理干预"


def _neg_fires(text: str) -> bool:
    return any(value > 0 for value in remove_by_conditions(text))


def _estimated_level(line: str) -> int:
    level, _reason, _cleaned = _estimate_markdown_heading_level(line, None)
    return int(level)


def test_mid_sentence_terminator_triggers_neg() -> None:
    assert _neg_fires("3.1 供给侧视角。市场规模持续增长")


def test_line_end_terminator_triggers_neg() -> None:
    assert _neg_fires("3.2.1 供给侧视角与需求侧感知存在感知偏差。")


def test_clean_heading_does_not_trigger_neg() -> None:
    assert not _neg_fires("3.2.2 筛查工具")


def test_real_fused_heading_lines_match_production_doc_nav() -> None:
    assert _estimated_level(_REAL_3_CONSENSUS) > 0
    assert _estimated_level(_REAL_322_SCREENING_TOOLS) > 0
    assert _estimated_level(_REAL_35_INTERVENTION) > 0
    assert _estimated_level(_REAL_321_SCREENING_TARGET) == -1
    assert _estimated_level(_REAL_331_ASSESSMENT_TARGET) == -1
