"""Offline fatal-native regressions at real dispatch and fake provider boundaries."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-kw04-unit")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from shared.core.exceptions.domain_exceptions import UnavailableException
from shared.services.retrieval.agent_explore import dispatch
from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_explore.harness import cursor_harness, openai_harness
from shared.services.retrieval.agent_explore.types import EpisodeResult
from shared.services.retrieval.agent_tools import registry
from shared.services.retrieval.agent_tools.registry import ToolContext, ToolRegistry, ToolResult, ToolSpec
from shared.services.retrieval.corpus_revision_context import CorpusRevisionContext
from shared.services.retrieval.execution.revision_pins import RetrievalRevisionPins
from shared.services.retrieval.lexical.error import LexicalIndexError


@asynccontextmanager
async def openDatabase() -> AsyncIterator[AsyncSession]:
    async with AsyncSession() as database:
        yield database


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> tuple[ToolRegistry, list[str], LexicalIndexError]:
    calls: list[str] = []
    failure: LexicalIndexError = LexicalIndexError("native service disconnected", isRetryable=True)
    toolRegistry: ToolRegistry = ToolRegistry()

    async def capturePins(*arguments: object, **keywords: object) -> RetrievalRevisionPins:
        return RetrievalRevisionPins({"doc": "rev"})
    monkeypatch.setattr(registry, "capture_revision_pins", capturePins)

    async def recall(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
        assert dict(CorpusRevisionContext.get_pins() or {}) == {"doc": "rev"}
        calls.append("recall")
        raise failure

    async def grep(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
        assert dict(CorpusRevisionContext.get_pins() or {}) == {"doc": "rev"}
        calls.append("grep")
        return ToolResult(text="earlier evidence", payload={"rows": [{"document_id": "doc", "section_path": "A"}]})

    for name, handler in (("corpus.recall", recall), ("corpus.grep", grep)):
        toolRegistry.register(ToolSpec(name=name, description=name, json_schema={"type": "object", "properties": {}, "additionalProperties": False}, run=handler))
    monkeypatch.setattr(dispatch, "REGISTRY", toolRegistry)
    monkeypatch.setattr(openai_harness, "REGISTRY", toolRegistry)
    monkeypatch.setattr(cursor_harness, "REGISTRY", toolRegistry)
    return toolRegistry, calls, failure


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["registry", "dispatch"])
async def test_native_failure_survives_every_tool_wrapper(tools: tuple[ToolRegistry, list[str], LexicalIndexError], layer: str) -> None:
    toolRegistry, calls, failure = tools
    with CorpusRevisionContext.bind(RetrievalRevisionPins({"doc": "rev"})):
        with pytest.raises(LexicalIndexError) as caught:
            if layer == "registry":
                async with openDatabase() as database:
                    await toolRegistry.dispatch("corpus.recall", ToolContext(db=database, user_id="user", namespace="ns", db_factory=openDatabase), {})
            else:
                await dispatch.dispatch_tool_call("corpus.recall", {}, db_factory=openDatabase, user_id="user", namespace="ns")
    assert caught.value is failure
    assert calls == ["recall"]


class FakeOpenAI:
    def __init__(self, names: list[list[str]]) -> None:
        self.turns: list[list[str]] = names
        self.calls: int = 0

    def chat_completion_raw_with_usage(self, **arguments: object) -> tuple[SimpleNamespace, dict[str, int]]:
        self.calls += 1
        names: list[str] = self.turns.pop(0)
        calls: list[SimpleNamespace] = [SimpleNamespace(function=SimpleNamespace(name=name, arguments="{}")) for name in names]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=calls, content=""))]), {"total_tokens": 1}


@pytest.mark.asyncio
async def test_openai_stops_on_fatal_after_evidence(tools: tuple[ToolRegistry, list[str], LexicalIndexError], monkeypatch: pytest.MonkeyPatch) -> None:
    _, calls, failure = tools
    provider: FakeOpenAI = FakeOpenAI([["corpus_grep"], ["corpus_recall", "corpus_grep"], ["finish"]])
    monkeypatch.setattr(openai_harness, "_resolve_client_and_model", lambda: (provider, "offline"))
    with CorpusRevisionContext.bind(RetrievalRevisionPins({"doc": "rev"})):
        with pytest.raises(LexicalIndexError) as caught:
            await openai_harness.OpenAIHarness().run_episode(db_factory=openDatabase, user_id="user", namespace="ns", query="query", budget=EpisodeBudget())
    assert caught.value is failure
    assert calls == ["grep", "recall"]
    assert provider.calls == 2


class FakeCursor:
    """Models SDK callback exception conversion and post-failure callbacks."""

    def __init__(self) -> None:
        self.customTools: dict[str, SimpleNamespace] = {}
        self.callback: object = None
        self.outputs: list[object] = []
        self.cancelled: bool = False
        self.completed: bool = False
        self.failed: asyncio.Event = asyncio.Event()

    async def __aenter__(self) -> FakeCursor:
        return self

    async def __aexit__(self, *arguments: object) -> None:
        return None

    async def launch_bridge(self, **arguments: object) -> FakeCursor:
        return self

    @property
    def agents(self) -> FakeCursor:
        return self

    async def create(self, options: SimpleNamespace) -> FakeCursor:
        self.customTools = options.local.custom_tools
        return self

    async def send(self, prompt: str, options: SimpleNamespace) -> FakeCursor:
        self.callback = options.on_delta
        return self

    async def execute(self, name: str, number: int) -> None:
        callback = cast(object, self.callback)
        assert callable(callback)
        callback(SimpleNamespace(type="tool-call-started", call_id=str(number), model_call_id=str(number)))
        # run_in_executor deliberately does not inherit ContextVars, like SDK threads.
        try:
            output: object = await asyncio.get_running_loop().run_in_executor(None, self.customTools[name].execute, {}, SimpleNamespace(tool_call_id=str(number)))
            self.outputs.append(output)
        except Exception as error:
            self.outputs.append(error)

    async def wait(self) -> SimpleNamespace:
        await self.execute("corpus_grep", 1)
        await self.execute("corpus_recall", 2)
        self.failed.set()
        await self.execute("corpus_grep", 3)
        await self.execute("corpus_pick", 4)
        await self.execute("finish", 5)
        self.completed = True
        return SimpleNamespace(usage=SimpleNamespace(total_tokens=1))

    async def cancel(self) -> None:
        self.cancelled = True

    def sdk(self) -> SimpleNamespace:
        return SimpleNamespace(AsyncClient=self, AgentOptions=lambda **args: SimpleNamespace(**args), LocalAgentOptions=lambda **args: SimpleNamespace(**args), SendOptions=lambda **args: SimpleNamespace(**args), CustomTool=lambda **args: SimpleNamespace(**args))


@pytest.mark.asyncio
async def test_cursor_rejects_late_callbacks_even_when_sdk_swallows_fatal(tools: tuple[ToolRegistry, list[str], LexicalIndexError], monkeypatch: pytest.MonkeyPatch) -> None:
    _, calls, failure = tools
    provider: FakeCursor = FakeCursor()
    monkeypatch.setenv("CURSOR_API_KEY", "offline-test-key")
    monkeypatch.setattr(cursor_harness, "_require_cursor_sdk", provider.sdk)
    with CorpusRevisionContext.bind(RetrievalRevisionPins({"doc": "rev"})):
        with pytest.raises(LexicalIndexError) as caught:
            await cursor_harness.CursorHarness(model="offline").run_episode(db_factory=openDatabase, user_id="user", namespace="ns", query="query", budget=EpisodeBudget(wall_clock_seconds=5))
    assert caught.value is failure
    for wireName in ("corpus_grep", "corpus_pick", "finish"):
        with pytest.raises(LexicalIndexError) as late:
            await asyncio.get_running_loop().run_in_executor(None, provider.customTools[wireName].execute, {}, SimpleNamespace(tool_call_id="late"))
        assert late.value is failure
    assert calls == ["grep", "recall"]
    assert provider.cancelled
    assert all('"status": "finished"' not in str(output) for output in provider.outputs)


@pytest.mark.asyncio
@pytest.mark.parametrize("doesWrapperReplaceError", [False, True])
async def test_swallowed_fatal_cannot_reach_assembly_or_cache(tools: tuple[ToolRegistry, list[str], LexicalIndexError], monkeypatch: pytest.MonkeyPatch, doesWrapperReplaceError: bool) -> None:
    from shared.core.config import settings
    from shared.services.retrieval.agent_explore import harness
    from shared.services.retrieval.execution import plan, routes
    from shared.services.retrieval.execution.query_request import RetrievalQuery
    from shared.services.retrieval.lexical.asyncread import AsyncPublishedLexicalRead

    _, calls, failure = tools
    engine = create_async_engine("postgresql+asyncpg://test:test@localhost/test")
    writes: list[str] = []

    async def start(reader: AsyncPublishedLexicalRead) -> None:
        reader.pins = RetrievalRevisionPins({"doc": "rev"})
        reader._hasStarted = True

    async def cacheRead(**arguments: object) -> tuple[int, None]:
        return 1, None

    async def cacheWrite(**arguments: object) -> None:
        writes.append("cache")

    async def smallCorpus(context: object) -> None:
        return None

    async def failAssembly(**arguments: object) -> object:
        writes.append("assembly")
        raise AssertionError("fatal reached successful result assembly")

    async def runEpisode(**arguments: object) -> EpisodeResult:
        await dispatch.dispatch_tool_call("corpus.grep", {}, db_factory=openDatabase, user_id="user", namespace="ns")
        try:
            await dispatch.dispatch_tool_call("corpus.recall", {}, db_factory=openDatabase, user_id="user", namespace="ns")
        except Exception:
            if doesWrapperReplaceError:
                raise RuntimeError("provider replaced callback failure")
        return EpisodeResult(pool=[])

    monkeypatch.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.setattr(AsyncPublishedLexicalRead, "startAsync", start)
    monkeypatch.setattr(plan, "_read_cached_response", cacheRead)
    monkeypatch.setattr(plan, "_write_cached_response", cacheWrite)
    monkeypatch.setattr(routes, "_try_run_small_corpus_route", smallCorpus)
    monkeypatch.setattr(routes, "assemble_retrieval_results", failAssembly)
    monkeypatch.setattr(harness, "resolve_harness", lambda **arguments: SimpleNamespace(run_episode=runEpisode))
    # Avoid a database connection even if the buggy route attempts assembly.
    monkeypatch.setattr(routes, "open_fresh_database_context", openDatabase)
    async def resolveReferences(**arguments: object) -> SimpleNamespace:
        return SimpleNamespace(rows=[], refs=[])
    monkeypatch.setattr(routes, "resolve_workflow_references", resolveReferences)
    async with AsyncSession(engine) as database:
        request: RetrievalQuery = RetrievalQuery(db=database, user_id="user", namespace="ns", query="query", top_k=1, exclude_document_ids=[], exclude_sections=[])
        with pytest.raises(UnavailableException) as caught:
            await plan.RetrievalExecutionPlan(request).execute()
    await engine.dispose()
    assert caught.value.original_exception is failure
    assert writes == []
    assert calls == ["grep", "recall"]


@pytest.mark.asyncio
async def test_tool_validation_and_empty_match_remain_recoverable(tools: tuple[ToolRegistry, list[str], LexicalIndexError], monkeypatch: pytest.MonkeyPatch) -> None:
    toolRegistry, calls, _ = tools

    async def noMatch(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
        calls.append("no_match")
        return ToolResult(text="", payload={"rows": []})

    toolRegistry.register(ToolSpec(name="corpus.no_match", description="empty search", json_schema={"type": "object", "properties": {}, "additionalProperties": False}, run=noMatch))
    with CorpusRevisionContext.bind(RetrievalRevisionPins({"doc": "rev"})):
        invalid: ToolResult = await dispatch.dispatch_tool_call("corpus.no_match", {"unknown": 1}, db_factory=openDatabase, user_id="user", namespace="ns")
        assert invalid.error is not None
        assert calls == []
        provider: FakeOpenAI = FakeOpenAI([["corpus_no_match"], ["finish"]])
        monkeypatch.setattr(openai_harness, "_resolve_client_and_model", lambda: (provider, "offline"))
        episode: EpisodeResult = await openai_harness.OpenAIHarness().run_episode(db_factory=openDatabase, user_id="user", namespace="ns", query="query", budget=EpisodeBudget())
    assert episode.pool == []
    assert episode.stop_reason == "finished"
    assert calls == ["no_match"]
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_cursor_preserves_captured_revision_context_in_sdk_threads(tools: tuple[ToolRegistry, list[str], LexicalIndexError], monkeypatch: pytest.MonkeyPatch) -> None:
    _, calls, failure = tools

    async def rejectRecapture(*arguments: object, **keywords: object) -> RetrievalRevisionPins:
        raise AssertionError("SDK callback lost the captured revision pins")

    monkeypatch.setattr(registry, "capture_revision_pins", rejectRecapture)
    provider: FakeCursor = FakeCursor()
    monkeypatch.setenv("CURSOR_API_KEY", "offline-test-key")
    monkeypatch.setattr(cursor_harness, "_require_cursor_sdk", provider.sdk)
    with CorpusRevisionContext.bind(RetrievalRevisionPins({"doc": "rev"})):
        with pytest.raises(LexicalIndexError) as caught:
            await cursor_harness.CursorHarness(model="offline").run_episode(db_factory=openDatabase, user_id="user", namespace="ns", query="query", budget=EpisodeBudget(wall_clock_seconds=5))
    assert caught.value is failure
    assert calls == ["grep", "recall"]


@pytest.mark.asyncio
async def test_cursor_cancels_provider_that_keeps_waiting_after_fatal(tools: tuple[ToolRegistry, list[str], LexicalIndexError], monkeypatch: pytest.MonkeyPatch) -> None:
    _, calls, failure = tools

    class WaitingCursor(FakeCursor):
        async def wait(self) -> SimpleNamespace:
            await self.execute("corpus_recall", 1)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    provider: WaitingCursor = WaitingCursor()
    monkeypatch.setenv("CURSOR_API_KEY", "offline-test-key")
    monkeypatch.setattr(cursor_harness, "_require_cursor_sdk", provider.sdk)
    with CorpusRevisionContext.bind(RetrievalRevisionPins({"doc": "rev"})):
        with pytest.raises(LexicalIndexError) as caught:
            await asyncio.wait_for(cursor_harness.CursorHarness(model="offline").run_episode(db_factory=openDatabase, user_id="user", namespace="ns", query="query", budget=EpisodeBudget(wall_clock_seconds=5)), timeout=1)
    assert caught.value is failure
    assert calls == ["recall"]
    assert provider.cancelled


@pytest.mark.asyncio
async def test_cursor_continues_after_an_ordinary_empty_match(tools: tuple[ToolRegistry, list[str], LexicalIndexError], monkeypatch: pytest.MonkeyPatch) -> None:
    toolRegistry, calls, _ = tools
    async def noMatch(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
        calls.append("no_match")
        return ToolResult(text="", payload={"rows": []})
    toolRegistry.register(ToolSpec(name="corpus.no_match", description="empty search", json_schema={"type": "object", "properties": {}, "additionalProperties": False}, run=noMatch))
    class EmptyCursor(FakeCursor):
        async def wait(self) -> SimpleNamespace:
            await self.execute("corpus_no_match", 1)
            await self.execute("finish", 2)
            return SimpleNamespace(usage=SimpleNamespace(total_tokens=1))
    provider: EmptyCursor = EmptyCursor()
    monkeypatch.setenv("CURSOR_API_KEY", "offline-test-key")
    monkeypatch.setattr(cursor_harness, "_require_cursor_sdk", provider.sdk)
    with CorpusRevisionContext.bind(RetrievalRevisionPins({"doc": "rev"})):
        episode: EpisodeResult = await cursor_harness.CursorHarness(model="offline").run_episode(db_factory=openDatabase, user_id="user", namespace="ns", query="query", budget=EpisodeBudget(wall_clock_seconds=5))
    assert episode.pool == []
    assert episode.stop_reason == "finished"
    assert calls == ["no_match"]
    assert not provider.cancelled
    assert '"status": "finished"' in str(provider.outputs[-1])


@pytest.mark.asyncio
async def test_native_request_without_async_bind_returns_canonical_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.core.config import settings
    from shared.services.retrieval.execution.plan import RetrievalExecutionPlan
    from shared.services.retrieval.execution.query_request import RetrievalQuery
    monkeypatch.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "opensearch")
    async with AsyncSession() as database:
        request: RetrievalQuery = RetrievalQuery(db=database, user_id="user", namespace="ns", query="query", top_k=1, exclude_document_ids=[], exclude_sections=[])
        with pytest.raises(UnavailableException):
            await RetrievalExecutionPlan(request).execute()
