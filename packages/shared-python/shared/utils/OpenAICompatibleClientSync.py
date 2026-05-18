"""Compatibility wrapper for the OpenAI-compatible sync client."""

from shared.services.ai.openai_compatible_client_sync import (
    OpenAICompatibleClientSync,
    get_openai_client,
)

__all__ = ["OpenAICompatibleClientSync", "get_openai_client"]
