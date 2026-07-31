from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.page_memory.page_plan import PagePlan, PageProcessingStrategy
from app.services.page_memory.page_renderer import PageRenderResult
from app.services.page_memory.page_tagger import (
    PageTagResult,
    _completion_tokens,
    _parse_combined_page_tag_response as _parse_page_tag_response,
    _response_truncated,
    _summarize_page_text,
    _tag_vlm_combined,
    _tag_vlm_titles,
    tag_pages,
)
from shared.core.exceptions.domain_exceptions import UnavailableException
from shared.services.ai.prompt_service import build_prompt


def _write_page_image(tmp_path, page_index: int) -> str:
    image_path = tmp_path / f"page-{page_index}.png"
    image_path.write_bytes(b"png")
    return str(image_path)


def _page(tmp_path, page_index: int) -> PageRenderResult:
    return PageRenderResult(
        page_index=page_index,
        image_path=_write_page_image(tmp_path, page_index),
        raw_text="",
        width=100,
        height=200,
        is_landscape=False,
    )


def _plan(page_index: int) -> PagePlan:
    return PagePlan(
        page_index=page_index,
        strategy=PageProcessingStrategy.VLM_PAGE,
        reason="test",
    )


def test_response_truncated_detects_budget_hit_and_incomplete_json() -> None:
    assert _response_truncated(
        '{"titles":[',
        usage={"completion_tokens": 800},
        max_tokens=800,
    )
    assert _response_truncated(
        '{"titles":[{"text":"A"',
        usage={"completion_tokens": 120},
        max_tokens=800,
    )
    assert not _response_truncated(
        '{"titles":[],"summary":"","entities":[]}',
        usage={"completion_tokens": 40},
        max_tokens=800,
    )
    assert _completion_tokens({"completion_tokens": 12}) == 12


def test_combined_response_populates_all_page_fields() -> None:
    result = _parse_page_tag_response(
        """
        {
          "titles": [
            {"text": "1 Scope", "is_in_table": false, "is_in_header_footer": false},
            {"text": "Table Header", "is_in_table": true, "is_in_header_footer": false}
          ],
          "summary": "Scope requirements.",
          "entities": [{"text": "Authority", "type": "organization"}]
        }
        """,
        page_index=8,
    )

    assert result.page_index == 8
    assert result.summary == "Scope requirements."
    assert result.keywords == ["Authority"]
    assert result.entities == [{"text": "Authority", "type": "organization"}]
    assert [item["text"] for item in result.observed_titles] == ["1 Scope"]
    assert result.strategy_used == "vlm_page"


def test_combined_prompt_applies_mixed_page_boundary_once() -> None:
    prompt, *_ = build_prompt(
        "page-memory-vlm-page",
        "",
        "",
        paras={
            "body_start_text": "1 Introduction",
            "scan_direction": "top_to_bottom_left_to_right",
        },
    )

    assert prompt.count("BOUNDARY PAGE:") == 1
    assert '"1 Introduction"' in prompt
    assert '"titles"' in prompt
    assert '"summary"' in prompt
    assert '"entities"' in prompt


def test_visual_entity_prompt_excludes_page_chrome_and_structure_codes() -> None:
    prompt, *_ = build_prompt("page-memory-vlm-page", "", "", paras={})

    assert "running headers and footers" in prompt
    assert "section headings and outline numbering codes" in prompt
    assert "cross-references to other parts" in prompt
    assert "clause/part/table/spec numbers" in prompt


def test_text_entity_prompt_does_not_claim_visual_layout_filtering() -> None:
    prompt, *_ = build_prompt(
        "page-memory-text-page",
        "Acme operates in Sydney.",
        "",
        paras={},
    )

    normalized_prompt = " ".join(prompt.split())
    assert "plain text only" in normalized_prompt
    assert "do not claim to identify headers" in normalized_prompt
    assert "in one or two" in normalized_prompt
    assert "in in one or two" not in normalized_prompt


def test_combined_tag_escalates_budget_on_truncated_json(
    monkeypatch,
    tmp_path,
) -> None:
    truncated = '{"titles":[{"text":"Section A"'
    complete = (
        '{"titles":[{"text":"Section A","is_in_table":false,'
        '"is_in_header_footer":false}],'
        '"summary":"Summary","entities":[]}'
    )
    calls: list[int] = []

    class _FakeClient:
        def chat_completion_with_usage(self, **kwargs):
            max_tokens = int(kwargs["max_tokens"])
            calls.append(max_tokens)
            if max_tokens == 800:
                return truncated, {"completion_tokens": 800, "prompt_tokens": 10}
            return complete, {"completion_tokens": 180, "prompt_tokens": 10}

    monkeypatch.setattr(
        "shared.services.ai.llm_overrides.get_vision_client",
        lambda requested_model=None: (_FakeClient(), requested_model or "fake-vlm"),
    )
    monkeypatch.setattr(
        "app.services.page_memory.page_tagger.build_prompt",
        lambda *args, **kwargs: ("prompt", 0.0, 0.01, 800),
    )

    result = _tag_vlm_combined(_page(tmp_path, 38), model="fake-vlm")

    assert calls == [800, 1200]
    assert [item["text"] for item in result.observed_titles] == ["Section A"]
    assert result.summary == "Summary"


def test_combined_tagging_preserves_page_assignment_under_concurrency(
    monkeypatch,
    tmp_path,
) -> None:
    import gevent

    def _fake_tag_vlm_combined(
        page: PageRenderResult,
        *,
        model: str,
        scan_direction: str = "top_to_bottom_left_to_right",
        body_start_text: str = "",
    ) -> PageTagResult:
        gevent.sleep(0.01 * (4 - page.page_index))
        return PageTagResult(
            page_index=page.page_index,
            summary=f"summary-{page.page_index}",
            observed_titles=[{"text": f"title-{page.page_index}"}],
            strategy_used="vlm_page",
        )

    monkeypatch.setitem(
        tag_pages.__globals__,
         "_tag_vlm_combined",
        _fake_tag_vlm_combined,
    )
    pages = [_page(tmp_path, page_index) for page_index in [1, 2, 3]]

    results = tag_pages(
        pages=pages,
        plans=[_plan(page_index) for page_index in [1, 2, 3]],
        vlm_model="fake-vlm",
        max_concurrent=2,
    )

    assert [result.page_index for result in results] == [1, 2, 3]
    assert {
        result.page_index: result.observed_titles[0]["text"] for result in results
    } == {1: "title-1", 2: "title-2", 3: "title-3"}


def test_combined_tagging_respects_max_concurrent_cap(monkeypatch, tmp_path) -> None:
    import gevent

    inflight = {"count": 0, "peak": 0}

    def _fake_tag_vlm_combined(
        page: PageRenderResult,
        **_kwargs,
    ) -> PageTagResult:
        inflight["count"] += 1
        inflight["peak"] = max(inflight["peak"], inflight["count"])
        gevent.sleep(0.02)
        inflight["count"] -= 1
        return PageTagResult(page_index=page.page_index, strategy_used="vlm_page")

    monkeypatch.setitem(tag_pages.__globals__, "_tag_vlm_combined", _fake_tag_vlm_combined)
    pages = [_page(tmp_path, page_index) for page_index in range(1, 9)]

    results = tag_pages(
        pages=pages,
        plans=[_plan(page_index) for page_index in range(1, 9)],
        vlm_model="fake-vlm",
        max_concurrent=5,
    )

    assert [result.page_index for result in results] == list(range(1, 9))
    assert inflight["peak"] <= 5


def test_combined_tagging_failed_greenlet_fails_stage(monkeypatch, tmp_path) -> None:
    def _fake_tag_vlm_combined(
        page: PageRenderResult,
        **_kwargs,
    ) -> PageTagResult:
        if page.page_index == 2:
            raise RuntimeError("page tagging failed")
        return PageTagResult(page_index=page.page_index, strategy_used="vlm_page")

    monkeypatch.setitem(tag_pages.__globals__, "_tag_vlm_combined", _fake_tag_vlm_combined)

    with pytest.raises(RuntimeError):
        tag_pages(
            pages=[_page(tmp_path, 1), _page(tmp_path, 2)],
            plans=[_plan(1), _plan(2)],
            vlm_model="fake-vlm",
            max_concurrent=2,
        )


def test_combined_tagging_unavailable_exception_propagates(
    monkeypatch,
    tmp_path,
) -> None:
    def _fake_tag_vlm_combined(
        page: PageRenderResult,
        **_kwargs,
    ) -> PageTagResult:
        raise UnavailableException(
            internal_message="capacity busy",
            retry_after=5,
        )

    monkeypatch.setitem(tag_pages.__globals__, "_tag_vlm_combined", _fake_tag_vlm_combined)

    with pytest.raises(UnavailableException):
        tag_pages(
            pages=[_page(tmp_path, 1)],
            plans=[_plan(1)],
            vlm_model="fake-vlm",
            max_concurrent=1,
        )


def test_text_mode_uses_title_vlm_and_text_summary(monkeypatch, tmp_path) -> None:
    title_pages: list[int] = []
    summary_pages: list[int] = []

    def _fake_titles(page, **_kwargs):
        title_pages.append(page.page_index)
        return PageTagResult(
            page_index=page.page_index,
            observed_titles=[{"text": f"Title {page.page_index}"}],
            strategy_used="vlm_titles",
            tagging_mode="text",
        )

    def _fake_summary(page, **_kwargs):
        summary_pages.append(page.page_index)
        return f"summary-{page.page_index}", [
            {"text": "Alice", "type": "person"}
        ]

    monkeypatch.setitem(tag_pages.__globals__, "_tag_vlm_titles", _fake_titles)
    monkeypatch.setitem(tag_pages.__globals__, "_summarize_page_text", _fake_summary)

    pages = [
        PageRenderResult(
            page_index=page_index,
            image_path=_write_page_image(tmp_path, page_index),
            raw_text=f"body {page_index}",
            width=100,
            height=200,
            is_landscape=False,
        )
        for page_index in (1, 2)
    ]
    results = tag_pages(
        pages=pages,
        plans=[_plan(1), _plan(2)],
        vlm_model="fake-vlm",
        tagging_mode="text",
        text_summary_concurrency=2,
        max_concurrent=2,
    )

    assert title_pages == [1, 2]
    assert summary_pages == [1, 2]
    assert [result.strategy_used for result in results] == ["text_page", "text_page"]
    assert [result.summary for result in results] == ["summary-1", "summary-2"]
    assert results[0].entities == [{"text": "Alice", "type": "person"}]
    assert results[0].keywords == ["Alice"]
    assert [item["text"] for item in results[0].observed_titles] == ["Title 1"]


def test_text_mode_summarizes_when_title_detection_skips(monkeypatch, tmp_path) -> None:
    summary_pages: list[int] = []

    monkeypatch.setitem(
        tag_pages.__globals__,
        "_tag_vlm_titles",
        lambda page, **_kwargs: PageTagResult(
            page_index=page.page_index,
            strategy_used="skip_tagging",
            tagging_mode="text",
        ),
    )

    def _fake_summary(page, **_kwargs):
        summary_pages.append(page.page_index)
        return "body summary", []

    monkeypatch.setitem(tag_pages.__globals__, "_summarize_page_text", _fake_summary)
    page = PageRenderResult(
        page_index=1,
        image_path=_write_page_image(tmp_path, 1),
        raw_text="readable body",
        width=100,
        height=200,
        is_landscape=False,
    )

    result = tag_pages(
        pages=[page],
        plans=[_plan(1)],
        vlm_model="fake-vlm",
        tagging_mode="text",
        max_concurrent=1,
        text_summary_concurrency=1,
    )[0]

    assert summary_pages == [1]
    assert result.strategy_used == "text_page"
    assert result.tagging_mode == "text"
    assert result.summary == "body summary"


def test_text_mode_respects_summary_concurrency(monkeypatch, tmp_path) -> None:
    import gevent

    inflight = {"count": 0, "peak": 0}
    monkeypatch.setitem(
        tag_pages.__globals__,
        "_tag_vlm_titles",
        lambda page, **_kwargs: PageTagResult(
            page_index=page.page_index,
            strategy_used="vlm_titles",
            tagging_mode="text",
        ),
    )

    def _fake_summary(page, **_kwargs):
        inflight["count"] += 1
        inflight["peak"] = max(inflight["peak"], inflight["count"])
        gevent.sleep(0.02)
        inflight["count"] -= 1
        return f"summary-{page.page_index}", []

    monkeypatch.setitem(tag_pages.__globals__, "_summarize_page_text", _fake_summary)
    pages = [
        PageRenderResult(
            page_index=page_index,
            image_path=_write_page_image(tmp_path, page_index),
            raw_text=f"body {page_index}",
            width=100,
            height=200,
            is_landscape=False,
        )
        for page_index in range(1, 7)
    ]

    tag_pages(
        pages=pages,
        plans=[_plan(page_index) for page_index in range(1, 7)],
        vlm_model="fake-vlm",
        tagging_mode="text",
        max_concurrent=5,
        text_summary_concurrency=2,
    )

    assert inflight["peak"] <= 2


def test_text_summary_unavailable_degrades_to_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "shared.services.ai.summary.engine.summarize",
        lambda **_kwargs: (_ for _ in ()).throw(
            UnavailableException(
                internal_message="text capacity busy",
                retry_after=5,
            )
        ),
    )

    summary, entities = _summarize_page_text(
        _page(tmp_path, 1),
        model="fake-text-model",
        text="readable body",
    )

    assert summary == ""
    assert entities == []


def test_text_title_skip_preserves_text_mode(tmp_path) -> None:
    page = PageRenderResult(
        page_index=1,
        image_path=str(tmp_path / "missing.png"),
        raw_text="readable body",
        width=100,
        height=200,
        is_landscape=False,
    )

    result = _tag_vlm_titles(page, model="fake-vlm")

    assert result.strategy_used == "skip_tagging"
    assert result.tagging_mode == "text"


def test_text_mode_exposes_transient_ocr_text_for_node_assembly(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setitem(
        tag_pages.__globals__,
        "_tag_vlm_titles",
        lambda page, **_kwargs: PageTagResult(
            page_index=page.page_index,
            strategy_used="vlm_titles",
            tagging_mode="text",
        ),
    )
    monkeypatch.setitem(
        tag_pages.__globals__,
        "_resolve_page_text",
        lambda page, **_kwargs: f"ocr-body-{page.page_index}",
    )
    monkeypatch.setitem(
        tag_pages.__globals__,
        "_summarize_page_text",
        lambda page, **_kwargs: ("summary", []),
    )

    result = tag_pages(
        pages=[_page(tmp_path, 1)],
        plans=[_plan(1)],
        vlm_model="fake-vlm",
        tagging_mode="text",
        max_concurrent=1,
        text_summary_concurrency=1,
    )[0]

    assert result.resolved_body_text == "ocr-body-1"
