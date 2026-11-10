"""Submit bounded optional PG copies; durable pending state owns recovery."""

from shared.services.background import BackgroundWorkPool
from shared.services.retrieval.lexical.legacy import LegacyLexicalCopy

_COPY_POOL: BackgroundWorkPool | None = None


class LegacyCopyDispatch:
    @staticmethod
    def submit(buildId: str) -> bool:
        global _COPY_POOL
        if _COPY_POOL is None:
            _COPY_POOL = BackgroundWorkPool(name="lexical-pg-copy", maximumWorkers=2)
        def runCopy() -> None:
            LegacyLexicalCopy().run(buildId)
        return _COPY_POOL.trySubmit(runCopy)

    @staticmethod
    def close() -> None:
        global _COPY_POOL
        if _COPY_POOL is not None:
            _COPY_POOL.close()
            _COPY_POOL = None
