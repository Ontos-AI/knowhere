"""Unit tests for map-nav sync LLM backend (no live API)."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.retrieval import nav_llm_backend as backend
from shared.services.retrieval.nav.nav_llm import get_nav_chat_backend, nav_chat


def test_install_registers_backend() -> None:
    assert get_nav_chat_backend() is backend.nav_chat_sync_backend


def test_backend_forwards_thinking_extra_body() -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def chat_completion_with_usage(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return "ok", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}

    with (
        patch(
            "shared.services.ai.llm_overrides.resolve_text",
            return_value=("deepseek-v4-flash", None, None),
        ),
        patch(
            "shared.services.ai.openai_compatible_client_sync.get_openai_client",
            return_value=FakeClient(),
        ),
    ):
        out = backend.nav_chat_sync_backend(
            messages=[{"role": "user", "content": "hi"}],
            model="deepseek-v4-flash",
            temperature=0.0,
            max_tokens=256,
            extra={"extra_body": {"thinking": {"type": "disabled"}}},
            timeout=30,
        )

    assert out["content"] == "ok"
    assert out["usage"]["total_tokens"] == 3
    assert captured["kwargs"]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert captured["kwargs"]["model"] == "deepseek-v4-flash"


def test_nav_chat_uses_installed_backend() -> None:
    fake = MagicMock(
        return_value={
            "content": '{"ok": true}',
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    from shared.services.retrieval.nav import nav_llm as nav_llm_mod

    prev = nav_llm_mod.get_nav_chat_backend()
    try:
        nav_llm_mod.set_nav_chat_backend(fake)
        result = nav_chat(
            purpose="test",
            messages=[{"role": "user", "content": "x"}],
            model="deepseek-v4-flash",
            thinking_role="action",
        )
        assert result["content"] == '{"ok": true}'
        assert fake.called
        call_kw = fake.call_args.kwargs
        assert call_kw["extra"]["extra_body"]["thinking"]["type"] == "disabled"
    finally:
        nav_llm_mod.set_nav_chat_backend(prev)


def test_backend_uses_mapnav_model_when_model_empty() -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def chat_completion_with_usage(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            captured["kwargs"] = kwargs
            return "ok", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    with (
        patch(
            "shared.services.ai.llm_overrides.resolve_text",
            side_effect=lambda model: (model, None, None),
        ),
        patch(
            "shared.services.ai.openai_compatible_client_sync.get_openai_client",
            return_value=FakeClient(),
        ),
    ):
        backend.nav_chat_sync_backend(
            messages=[{"role": "user", "content": "hi"}],
            model="",
            temperature=0.0,
            max_tokens=64,
        )
    assert captured["kwargs"]["model"] == "deepseek-v4-flash"
