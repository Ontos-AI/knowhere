"""Keep scheduler fairness separate from enforcement of the indexing deadline."""

from types import SimpleNamespace

import pytest

from shared.services.retrieval.lexical import compilation_checkpoint
from shared.services.retrieval.lexical.compilation_checkpoint import CompilationCheckpoint
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.lexical.error import LexicalIndexError


@pytest.mark.asyncio
@pytest.mark.parametrize("isAsync", [False, True])
async def test_checkpoint_yields_periodically_but_checks_every_step(
    isAsync: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.lexical import deadline

    clock: list[float] = [0.0]
    yields: list[float] = []
    fakeTime = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(deadline, "time", fakeTime)
    monkeypatch.setattr(compilation_checkpoint, "time", fakeTime)

    def recordYield(seconds: float) -> None:
        assert seconds == 0
        yields.append(clock[0])

    async def recordAsyncYield(seconds: float) -> None:
        recordYield(seconds)

    monkeypatch.setattr(compilation_checkpoint, "gevent", SimpleNamespace(sleep=recordYield))
    monkeypatch.setattr(compilation_checkpoint, "asyncio", SimpleNamespace(sleep=recordAsyncYield))
    checkpoint: CompilationCheckpoint = CompilationCheckpoint(IndexingDeadline(0.0016))

    async def check() -> None:
        if isAsync:
            await checkpoint.checkAsync()
        else:
            checkpoint.check()

    for tick in range(16):
        clock[0] = tick / 10_000
        await check()
    assert yields == [0.0, 0.001]
    clock[0] = 0.0017
    with pytest.raises(LexicalIndexError, match="deadline exceeded"):
        await check()
    assert yields == [0.0, 0.001]
