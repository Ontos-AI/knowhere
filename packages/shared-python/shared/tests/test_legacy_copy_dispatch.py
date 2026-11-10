"""Background copy submission never joins work or loses durable pending intent."""

import os
from unittest.mock import Mock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-kw06-unit")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")


def test_copy_dispatch_leaves_saturated_work_for_maintenance(monkeypatch) -> None:
    from shared.services.retrieval.lexical import copy_dispatch
    pool = Mock()
    pool.trySubmit.return_value = False
    monkeypatch.setattr(copy_dispatch, "_COPY_POOL", pool)
    assert not copy_dispatch.LegacyCopyDispatch.submit("build")
    pool.trySubmit.assert_called_once()


def test_copy_dispatch_submits_independent_work_without_joining(monkeypatch) -> None:
    from shared.services.retrieval.lexical import copy_dispatch
    pool = Mock()
    pool.trySubmit.return_value = True
    copy = Mock()
    monkeypatch.setattr(copy_dispatch, "_COPY_POOL", pool)
    monkeypatch.setattr(copy_dispatch, "LegacyLexicalCopy", lambda: copy)
    assert copy_dispatch.LegacyCopyDispatch.submit("build")
    copy.run.assert_not_called()
    operation = pool.trySubmit.call_args.args[0]
    operation()
    copy.run.assert_called_once_with("build")
