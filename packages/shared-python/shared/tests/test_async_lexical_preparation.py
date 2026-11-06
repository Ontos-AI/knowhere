"""Exact lexical parity and cooperative preparation without a thread executor."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from time import perf_counter

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-prepare-unit")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.preparation import LexicalPreparation
from shared.services.retrieval.scoring.hierarchy import ProviderToolSpace
from shared.services.retrieval.scoring.knowhere_provider import KnowhereProvider, SectionRow, UnitRow
from shared.services.retrieval.scoring.score_units import build_score_units


@pytest.fixture
def mixedRows() -> tuple[list[SectionRow], list[UnitRow]]:
    sections: list[SectionRow] = [
        SectionRow("root", None, "Root", "Root", 0, "", 0),
        SectionRow("parent", "root", "Annual Report / 财务", "财务 Finance", 1, "", 1),
        SectionRow("income", "parent", "Annual Report / 财务 / 收入", "收入 Revenue", 2, "", 2),
        SectionRow("risk", "parent", "Annual Report / 财务 / 风险", "风险 Risks", 2, "", 3),
        SectionRow("empty", "root", "Annual Report / 空节", "空节 Empty", 1, "", 4),
    ]
    chunks: list[UnitRow] = [
        UnitRow("heading", "parent", "text", "财务 Finance", 0),
        UnitRow("interstitial", "parent", "text", "Parent interstitial：总额 42万元.", 1),
        UnitRow("income-body", "income", "text", "2026 收入 revenue 2.5 million dollars.", 2, metadata={"connect_to": [{"target": "table"}]}),
        UnitRow("table", "root", "table", "tables/a.html", 3, file_path="tables/a.html", metadata={"asset_title": "收入表", "summary": "利润 10% profit"}),
        UnitRow("risk-body", "risk", "page", "Risk 风险模型 A/B. 财务及操作风险", 4),
        UnitRow("unresolved-image", "root", "image", "images/orphan.jpg", 5, metadata={"summary": "must not enter lexical evidence"}),
    ]
    return sections, chunks


@pytest.mark.asyncio
async def test_async_preparation_preserves_exact_mixed_language_units(mixedRows: tuple[list[SectionRow], list[UnitRow]]) -> None:
    sections, chunks = mixedRows
    expected = LexicalPreparation.prepare(documentId="document", revisionId="revision", sections=sections, chunks=chunks)
    actual = await LexicalPreparation.prepareAsync(documentId="document", revisionId="revision", sections=sections, chunks=chunks)
    assert actual == expected
    assert [(unit.pathTokens, unit.contentTokens) for unit in actual.units] == [
        ("root 财务 finance 收入 revenue 收入 revenue", "2026 收入 revenue 2.5 million dollars table tables/a.html 收入 利润 10 profit"),
        ("root 财务 finance 风险 risks 风险 risks", "risk 风险 模型 a/b 财务 操作 风险"),
        ("root 空节 empty 空节 empty", "空节 empty"),
        ("root 财务 finance 财务 finance", "财务 finance parent interstitial 总额 42 万元"),
    ]
    assert [unit.unitId for unit in actual.units] == ["income", "risk", "empty", "parent__self"]
    assert [unit.hasTable for unit in actual.units] == [True, False, False, False]
    assert all(not unit.hasImage for unit in actual.units)
    assert "must not enter" not in str(actual)


def test_raw_score_units_skip_tokenization_and_keep_title(mixedRows: tuple[list[SectionRow], list[UnitRow]], monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.services.retrieval.scoring import score_units
    def reject(*arguments: object, **keywords: object) -> str:
        raise AssertionError("raw unit generation tokenized text")
    monkeypatch.setattr(score_units, "build_path_search_text", reject)
    monkeypatch.setattr(score_units, "build_content_search_text", reject)
    sections, chunks = mixedRows
    provider = KnowhereProvider(doc_id="document", sections=sections, units=chunks)
    rows = build_score_units(ProviderToolSpace(provider), "document", tokenize=False)
    assert [row["chunk_id"] for row in rows] == ["income", "risk", "empty", "parent__self"]
    assert rows[0]["section_title"] == "收入 Revenue"
    assert "利润 10% profit" in rows[0]["content"]


@pytest.mark.asyncio
async def test_async_preparation_yields_between_cpu_units_without_executor(mixedRows: tuple[list[SectionRow], list[UnitRow]], monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.utils import text_utils
    def rejectExecutor(*arguments: object, **keywords: object) -> None:
        raise AssertionError("preparation used a thread executor")
    monkeypatch.setattr(asyncio, "to_thread", rejectExecutor)
    sections, chunks = mixedRows
    monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", rejectExecutor)
    beats: list[int] = [0]
    observed: list[int] = []
    finished: bool = False
    original = text_utils._tokenize_cjk_segment
    def compileContent(content: str) -> list[str]:
        observed.append(beats[0])
        started: float = perf_counter()
        while perf_counter() - started < 0.002:
            pass
        return original(content)
    monkeypatch.setattr(text_utils, "_tokenize_cjk_segment", compileContent)
    async def heartbeat() -> None:
        while not finished:
            beats[0] += 1
            await asyncio.sleep(0)
    task = asyncio.create_task(heartbeat())
    try:
        await LexicalPreparation.prepareAsync(documentId="document", revisionId="revision", sections=sections, chunks=chunks)
    finally:
        finished = True
        await task
    assert len(observed) >= 4 and len(set(observed)) == len(observed)



@pytest.mark.asyncio
async def test_async_preparation_rejects_oversized_raw_unit_before_tokenization(mixedRows: tuple[list[SectionRow], list[UnitRow]], monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.core.config import settings
    sections, chunks = mixedRows
    monkeypatch.setattr(settings, "KNOWHERE_INDEXING_MAXIMUM_UNIT_CHARACTERS", 1024)
    chunks[2] = replace(chunks[2], content="工程" * 1024)
    with pytest.raises(LexicalIndexError, match="limit"):
        await LexicalPreparation.prepareAsync(documentId="document", revisionId="revision", sections=sections, chunks=chunks)


@pytest.mark.asyncio
async def test_large_business_record_compiles_without_splitting_or_truncating_units(
    mixedRows: tuple[list[SectionRow], list[UnitRow]],
) -> None:
    sections, chunks = mixedRows
    # Representative of the frozen business fixture's 340,269-character row.
    chunks[2] = replace(chunks[2], content="provenance sample " * 20_000 + "finalmarker")
    expected = LexicalPreparation.prepare(documentId="document", revisionId="revision", sections=sections, chunks=chunks)
    prepared = await LexicalPreparation.prepareAsync(documentId="document", revisionId="revision", sections=sections, chunks=chunks)
    assert prepared == expected
    assert [unit.unitId for unit in prepared.units] == ["income", "risk", "empty", "parent__self"]
    tokens: list[str] = prepared.units[0].contentTokens.split()
    assert tokens.count("provenance") == 20_000 and "finalmarker" in tokens


@pytest.mark.asyncio
async def test_async_preparation_stops_expired_budget_before_more_units(mixedRows: tuple[list[SectionRow], list[UnitRow]]) -> None:
    sections, chunks = mixedRows
    deadline: IndexingDeadline = IndexingDeadline(0.001)
    await asyncio.sleep(0.005)
    with pytest.raises(LexicalIndexError):
        await LexicalPreparation.prepareAsync(documentId="document", revisionId="revision", sections=sections, chunks=chunks, deadline=deadline)


@pytest.mark.asyncio
async def test_async_compiler_preserves_legacy_heading_separator_tokens(mixedRows: tuple[list[SectionRow], list[UnitRow]]) -> None:
    sections, chunks = mixedRows
    sections[3] = replace(sections[3], section_title="风险 / Risks")
    result = await LexicalPreparation.prepareAsync(documentId="document", revisionId="revision", sections=sections, chunks=chunks)
    risk = next(unit for unit in result.units if unit.unitId == "risk")
    assert risk.pathTokens == "root 财务 finance 风险 risks risks"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["cycle", "deep"])
async def test_async_preparation_rejects_invalid_hierarchy_without_recursion(shape: str) -> None:
    sections: list[SectionRow] = [SectionRow(f"section-{number}", f"section-{number - 1}" if number else None,
        f"Path {number}", "Title", number, "", number) for number in range(300 if shape == "deep" else 2)]
    if shape == "cycle":
        sections[0] = replace(sections[0], parent_section_id="section-1")
    with pytest.raises(LexicalIndexError):
        await LexicalPreparation.prepareAsync(documentId="document", revisionId="revision", sections=sections, chunks=[])


def test_sync_preparation_checks_callback_during_structural_work(mixedRows: tuple[list[SectionRow], list[UnitRow]]) -> None:
    sections, chunks = mixedRows
    calls: list[int] = []
    def stop() -> None:
        calls.append(1)
        if len(calls) == 5:
            raise LexicalIndexError("checkpoint stopped compilation")
    with pytest.raises(LexicalIndexError, match="checkpoint"):
        LexicalPreparation.prepare(documentId="document", revisionId="revision", sections=sections, chunks=chunks, checkpoint=stop)
    assert len(calls) == 5


def test_sync_compiler_yields_to_other_gevent_work(mixedRows: tuple[list[SectionRow], list[UnitRow]]) -> None:
    import gevent
    sections, chunks = mixedRows
    beats: list[int] = []
    def heartbeat() -> None:
        for number in range(10):
            beats.append(number)
            gevent.sleep(0)
    pending = gevent.spawn(heartbeat)
    def checkpoint() -> None:
        gevent.sleep(0)
    actual = LexicalPreparation.prepare(documentId="document", revisionId="revision", sections=sections, chunks=chunks, checkpoint=checkpoint)
    pending.join()
    assert len(beats) == 10 and len(actual.units) == 4


@pytest.mark.asyncio
async def test_async_preparation_checks_aggregate_before_joining_or_tokenizing(mixedRows: tuple[list[SectionRow], list[UnitRow]], monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.core.config import settings
    from shared.services.retrieval.lexical import preparation
    sections, chunks = mixedRows
    monkeypatch.setattr(settings, "KNOWHERE_INDEXING_MAXIMUM_UNIT_CHARACTERS", 1024)
    chunks = [replace(chunks[2], content="甲" * 600, metadata={}), replace(chunks[2], chunk_id="second", content="乙" * 600, metadata={})]
    def reject(*arguments: object, **keywords: object) -> str:
        raise AssertionError("an oversized aggregate reached tokenization")
    monkeypatch.setattr(preparation, "build_content_search_text", reject)
    monkeypatch.setattr(preparation, "_iterate_path_search_text", reject)
    with pytest.raises(LexicalIndexError, match="limit"):
        await LexicalPreparation.prepareAsync(documentId="document", revisionId="revision", sections=sections, chunks=chunks)


@pytest.mark.asyncio
async def test_async_preparation_rejects_expensive_cjk_span_before_tokenizer(mixedRows: tuple[list[SectionRow], list[UnitRow]], monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.utils import text_utils
    sections, chunks = mixedRows
    chunks[2] = replace(chunks[2], content="甲" * 4097, metadata={})
    original = text_utils._tokenize_cjk_segment
    def rejectLargeSpan(text: str) -> list[str]:
        assert len(text) <= 4096, "oversized uninterrupted CJK span reached jieba"
        return original(text)
    monkeypatch.setattr(text_utils, "_tokenize_cjk_segment", rejectLargeSpan)
    with pytest.raises(LexicalIndexError, match="span"):
        await LexicalPreparation.prepareAsync(documentId="document", revisionId="revision", sections=sections, chunks=chunks)


@pytest.mark.asyncio
async def test_async_preparation_yields_between_many_bounded_cjk_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.utils import text_utils
    sections: list[SectionRow] = [SectionRow("section", None, "Path", "Title", 1, "", 0)]
    chunks: list[UnitRow] = [UnitRow("chunk", "section", "text", ("甲" * 4096 + " ") * 15, 0)]
    beats: list[int] = [0]
    observed: list[int] = []
    finished: bool = False
    def compileSpan(text: str) -> list[str]:
        observed.append(beats[0])
        started: float = perf_counter()
        while perf_counter() - started < 0.002:
            pass
        return ["甲乙"]
    monkeypatch.setattr(text_utils, "_tokenize_cjk_segment", compileSpan)
    async def heartbeat() -> None:
        while not finished:
            beats[0] += 1
            await asyncio.sleep(0)
    task = asyncio.create_task(heartbeat())
    try:
        await LexicalPreparation.prepareAsync(documentId="document", revisionId="revision", sections=sections, chunks=chunks)
    finally:
        finished = True
        await task
    assert len(observed) == 15 and len(set(observed)) == 15


@pytest.mark.asyncio
async def test_async_preparation_keeps_document_order_across_bounded_sort_runs() -> None:
    sections: list[SectionRow] = [SectionRow(f"section-{number:03}", None, f"Path{number}",
        f"Title{number}", 1, "", number % 5) for number in reversed(range(300))]
    chunks: list[UnitRow] = [UnitRow(f"chunk-{number:03}", f"section-{number:03}", "text",
        f"Body {number} 财务", number % 7) for number in reversed(range(300))]
    expected = LexicalPreparation.prepare(documentId="document", revisionId="revision", sections=sections, chunks=chunks)
    actual = await LexicalPreparation.prepareAsync(documentId="document", revisionId="revision", sections=sections, chunks=chunks)
    assert actual == expected
    assert [unit.unitId for unit in actual.units] == [row.section_id for row in sorted(sections, key=lambda row: (row.sort_order, row.section_id))]


@pytest.mark.asyncio
async def test_cjk_span_limit_applies_after_marker_removal_and_on_cache_hits() -> None:
    from shared.services.retrieval.publication_preparation_cache import PublicationPreparationCache
    content: str = "甲" * 2050 + "[images/a.png]" + "乙" * 2050
    sections: list[SectionRow] = [SectionRow("section", None, "Path", "Title", 1, "", 0)]
    chunks: list[UnitRow] = [UnitRow("chunk", "section", "text", content, 0)]
    with PublicationPreparationCache.scope(source_id="frozen-cjk-guard", content_version="1", index_format_version=2):
        PublicationPreparationCache.tokenize(content)
        with pytest.raises(LexicalIndexError, match="span"):
            await LexicalPreparation.prepareAsync(documentId="document", revisionId="revision", sections=sections, chunks=chunks)


def test_cancelled_token_stream_never_caches_partial_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.services.retrieval.publication_preparation_cache import PublicationPreparationCache
    from shared.utils import text_utils
    original = text_utils._tokenize_cjk_segment
    calls: list[str] = []
    def compileSpan(text: str) -> list[str]:
        calls.append(text)
        return original(text)
    monkeypatch.setattr(text_utils, "_tokenize_cjk_segment", compileSpan)
    with PublicationPreparationCache.scope(source_id="cancelled-token-stream", content_version="1", index_format_version=2):
        pending = PublicationPreparationCache.iterateTokenSteps("财务收入 风险模型")
        while next(pending) is None:
            pass
        pending.close()
        assert PublicationPreparationCache.tokenize("财务收入 风险模型") == ["财务", "收入", "风险", "模型"]
        assert calls == ["财务收入", "财务收入", "风险模型"]


def test_compiler_fingerprint_pins_effective_english_tokenizer_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.services.retrieval.lexical import preparation
    from shared.utils import text_utils

    originalVersion = preparation.version
    nativeVersion: list[str] = ["fixture-blingfire-version"]
    def getVersion(name: str) -> str:
        return nativeVersion[0] if name == "blingfire" else originalVersion(name)
    monkeypatch.setattr(preparation, "version", getVersion)
    try:
        monkeypatch.setattr(text_utils, "_blingfire_text_to_words", None)
        preparation._getCompilerFingerprint.cache_clear()
        fallback: str = LexicalPreparation.getCompilerFingerprint()
        monkeypatch.setattr(text_utils, "_blingfire_text_to_words", lambda text: text)
        preparation._getCompilerFingerprint.cache_clear()
        native: str = LexicalPreparation.getCompilerFingerprint()
        assert fallback != native, "Native English tokenization and syntok fallback must not share a compiler identity"
        nativeVersion[0] = "changed-blingfire-version"
        preparation._getCompilerFingerprint.cache_clear()
        assert LexicalPreparation.getCompilerFingerprint() != native
    finally:
        preparation._getCompilerFingerprint.cache_clear()
