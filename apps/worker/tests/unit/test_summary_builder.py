"""Unit tests for bottom-up doc_nav summary enrichment."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from app.services.connect_builder.summary_builder import (
    SUMMARY_MAX_LEN,
    _deterministic_section_summary,
    _llm_summarize,
    _recursive_summarize_nav,
    build_self_only_lookup,
)


def _leaf(title: str, summary: str = "", path: str = "") -> Dict[str, Any]:
    node: Dict[str, Any] = {"title": title, "summary": summary, "children": []}
    if path:
        node["path"] = path
    return node


def _parent(
    title: str,
    children: List[Dict[str, Any]],
    *,
    path: str = "",
    summary: str = "",
) -> Dict[str, Any]:
    node: Dict[str, Any] = {
        "title": title,
        "summary": summary,
        "children": children,
    }
    if path:
        node["path"] = path
    return node


class TestDeterministicAssembly:
    def test_order_covers_self_only_then_titles(self) -> None:
        text = _deterministic_section_summary(
            is_top_level=False,
            self_only="intro paragraph here",
            child_titles=["Alpha", "Beta"],
        )
        assert text.startswith("This section covers: ")
        assert "intro paragraph here" in text
        assert "Alpha, Beta" in text
        # self_only before titles
        assert text.index("intro paragraph here") < text.index("Alpha, Beta")

    def test_all_child_titles_even_when_summary_empty(self) -> None:
        parent = _parent(
            "Parent",
            [
                _leaf("HasText", summary="body"),
                _leaf("NoSummary", summary=""),
            ],
            path="doc.pdf/Parent",
        )
        result = _recursive_summarize_nav(
            parent,
            use_llm=False,
            source_file_name="doc.pdf",
        )
        assert "HasText" in result
        assert "NoSummary" in result
        assert result.startswith("This section covers: ")


class TestSelfOnlyLookup:
    def test_exact_path_only_excludes_descendants(self) -> None:
        chunks = [
            {
                "path": "doc.pdf/2.4.4 隐患治理",
                "content": "PARENT_INTRO_ONLY",
            },
            {
                "path": "doc.pdf/2.4.4 隐患治理/清单项A",
                "content": "CHILD_BODY_SHOULD_NOT_APPEAR",
            },
        ]
        lookup = build_self_only_lookup(chunks, source_file_name="doc.pdf")
        assert lookup["2.4.4 隐患治理"] == "PARENT_INTRO_ONLY"
        assert "CHILD_BODY_SHOULD_NOT_APPEAR" not in lookup["2.4.4 隐患治理"]
        assert "2.4.4 隐患治理 / 清单项A" in lookup

    def test_nonleaf_includes_self_only_in_deterministic(self) -> None:
        parent = _parent(
            "2.4.4 隐患治理",
            [
                _leaf("清单项A", summary="a", path="doc.pdf/2.4.4 隐患治理/清单项A"),
                _leaf("清单项B", summary="b", path="doc.pdf/2.4.4 隐患治理/清单项B"),
            ],
            path="doc.pdf/2.4.4 隐患治理",
        )
        lookup = build_self_only_lookup(
            [{"path": "doc.pdf/2.4.4 隐患治理", "content": "方案包括以下内容："}],
            source_file_name="doc.pdf",
        )
        result = _recursive_summarize_nav(
            parent,
            use_llm=False,
            self_only_lookup=lookup,
            source_file_name="doc.pdf",
        )
        assert "方案包括以下内容：" in result
        assert "清单项A" in result
        assert "清单项B" in result
        assert parent.get("self_summary") == "方案包括以下内容："


class TestLlmTrigger:
    def test_short_contrib_skips_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = {"n": 0}

        def _boom(**kwargs: Any) -> str:
            called["n"] += 1
            return "SHOULD_NOT_USE"

        monkeypatch.setattr(
            "app.services.connect_builder.summary_builder._llm_summarize",
            _boom,
        )
        parent = _parent(
            "P",
            [_leaf("A", summary="x"), _leaf("B", summary="y")],
            path="doc.pdf/P",
        )
        result = _recursive_summarize_nav(parent, use_llm=True, source_file_name="doc.pdf")
        assert called["n"] == 0
        assert result.startswith("This section covers: ")
        assert "A" in result and "B" in result

    def test_long_contrib_calls_llm_with_title_for_empty_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: Dict[str, Any] = {}

        def _fake_llm(**kwargs: Any) -> str:
            captured.update(kwargs)
            return "LLM_SUMMARY"

        monkeypatch.setattr(
            "app.services.connect_builder.summary_builder._llm_summarize",
            _fake_llm,
        )
        long_a = "A" * (SUMMARY_MAX_LEN + 5)
        parent = _parent(
            "P",
            [
                _leaf("HasSummary", summary=long_a),
                _leaf("EmptySummary", summary=""),
            ],
            path="doc.pdf/P",
        )
        lookup = {"P": "SELF_ONLY_INTRO"}
        # section path from doc.pdf/P is "P"
        result = _recursive_summarize_nav(
            parent,
            use_llm=True,
            self_only_lookup=lookup,
            source_file_name="doc.pdf",
        )
        assert result == "LLM_SUMMARY"
        assert captured["self_only"] == "SELF_ONLY_INTRO"
        titles = [t for t, _ in captured["child_rows"]]
        contribs = {t: c for t, c in captured["child_rows"]}
        assert "EmptySummary" in titles
        assert contribs["EmptySummary"] == "EmptySummary"
        assert contribs["HasSummary"] == long_a

    def test_single_child_with_self_only_does_not_copy_child(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "app.services.connect_builder.summary_builder._llm_summarize",
            lambda **kwargs: "MERGED",
        )
        long_child = "C" * (SUMMARY_MAX_LEN + 1)
        parent = _parent(
            "P",
            [_leaf("OnlyChild", summary=long_child)],
            path="doc.pdf/P",
        )
        result = _recursive_summarize_nav(
            parent,
            use_llm=True,
            self_only_lookup={"P": "intro"},
            source_file_name="doc.pdf",
        )
        assert result == "MERGED"
        assert result != long_child


class TestPromptPayload:
    def test_file_summary_prompt_contains_scope_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: Dict[str, Any] = {}

        def _fake_client() -> Any:
            class _C:
                def chat_completion(self, **kwargs: Any) -> str:
                    captured["messages"] = kwargs.get("messages")
                    return "ok"

            return _C()

        monkeypatch.setattr(
            "shared.services.ai.openai_compatible_client_sync.get_openai_client",
            _fake_client,
        )
        # Ensure build_prompt path works
        out = _llm_summarize(
            node_name="Parent",
            self_only="intro text",
            child_rows=[("ChildA", "summary A"), ("ChildB", "ChildB")],
            max_tokens=100,
        )
        assert out == "ok"
        user = captured["messages"][1]["content"]
        assert "SCOPE_TITLE: Parent" in user
        assert "SELF_ONLY_CONTENT:" in user
        assert "intro text" in user
        assert "COVERED_NODES:" in user
        assert "[ChildA] summary A" in user
        assert "[ChildB] ChildB" in user
        # legacy flat blob prompt removed
        assert "You will receive summaries of sub-sections" not in user


class TestDocNavTopSummaryPersistence:
    def test_enrich_persists_top_summary_and_defaults_top_llm(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        from app.services.connect_builder.summary_builder import (
            enrich_doc_nav_summaries,
            load_nav_top_summary,
        )

        captured: Dict[str, Any] = {}

        def _fake_llm(**kwargs: Any) -> str:
            captured["is_top"] = kwargs.get("node_name") == "Document Overview"
            captured["max_tokens"] = kwargs.get("max_tokens")
            captured["calls"] = int(captured.get("calls") or 0) + 1
            return "LLM document overview"

        monkeypatch.setattr(
            "app.services.connect_builder.summary_builder._llm_summarize",
            _fake_llm,
        )

        file_dir = tmp_path / "report.pdf"
        file_dir.mkdir()
        long_leaf = "L" * (SUMMARY_MAX_LEN + 5)
        doc_nav = {
            "version": "1.0",
            "file_name": "report.pdf",
            "stats": {},
            "sections": [
                {
                    "title": "Chapter 1",
                    "path": "report.pdf/Chapter 1",
                    "summary": long_leaf,
                    "chunk_count": 1,
                    "children": [],
                },
                {
                    "title": "Chapter 2",
                    "path": "report.pdf/Chapter 2",
                    "summary": long_leaf,
                    "chunk_count": 1,
                    "children": [],
                },
            ],
            "resources": {"images": [], "tables": []},
        }
        (file_dir / "doc_nav.json").write_text(
            json.dumps(doc_nav, ensure_ascii=False),
            encoding="utf-8",
        )

        results = enrich_doc_nav_summaries(
            str(tmp_path),
            source_file="report.pdf",
            use_llm=False,
            top_summary_use_llm=True,
        )
        assert results["report.pdf"] == "LLM document overview"
        assert captured["calls"] == 1
        assert captured["is_top"] is True

        saved = json.loads((file_dir / "doc_nav.json").read_text(encoding="utf-8"))
        assert saved["top_summary"] == "LLM document overview"
        # Section leaves keep original summaries; top LLM must not rewrite them.
        assert saved["sections"][0]["summary"] == long_leaf
        assert load_nav_top_summary(str(file_dir), "report.pdf") == (
            "LLM document overview"
        )
