"""Bring-your-own-key (BYOK) OpenAI-compatible LLM credentials."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


class LLMProviderConfig(BaseModel):
    """Credentials for one OpenAI-compatible provider endpoint."""

    api_key: str = Field(..., min_length=1, description="Provider API key")
    model: str = Field(..., min_length=1, description="Model identifier")
    base_url: str = Field(
        ...,
        min_length=1,
        description="OpenAI-compatible base URL (e.g. https://api.openai.com/v1)",
    )


class LLMConfig(BaseModel):
    """Optional text + vision provider configs for BYOK.

    Semantics:
    - ``provider`` set -> baseline for both text and vision channels
    - ``text`` / ``vision`` override that channel (and win over ``provider``)
    - a channel with neither a slot nor ``provider`` keeps server defaults
    - none of ``provider`` / ``text`` / ``vision`` set -> invalid when present

    Multimodal shorthand (one model for both channels)::

        {"provider": {"api_key": "...", "model": "gpt-4o", "base_url": "..."}}
    """

    provider: Optional[LLMProviderConfig] = Field(
        None,
        description=(
            "Shared OpenAI-compatible credentials for both text and vision. "
            "Use this for a single multimodal model; override with text/vision "
            "when channels need different endpoints."
        ),
    )
    text: Optional[LLMProviderConfig] = Field(
        None, description="Text / planning LLM credentials (overrides provider)"
    )
    vision: Optional[LLMProviderConfig] = Field(
        None, description="Vision / VLM credentials (overrides provider)"
    )

    @model_validator(mode="after")
    def _require_at_least_one_provider(self) -> "LLMConfig":
        if self.provider is None and self.text is None and self.vision is None:
            raise ValueError(
                "llm_config requires at least one of provider, text, or vision"
            )
        return self

    def text_effective(self) -> LLMProviderConfig | None:
        """Return the text-channel override, or None to keep server defaults."""
        return self.text if self.text is not None else self.provider

    def vision_effective(self) -> LLMProviderConfig | None:
        """Return the vision-channel override, or None to keep server defaults."""
        return self.vision if self.vision is not None else self.provider

    def masked_dump(self) -> dict[str, Any]:
        """Serialize with api_key values redacted for snapshots / responses."""
        from shared.utils.security_utils import mask_api_key

        def _mask_provider(provider: LLMProviderConfig | None) -> dict[str, Any] | None:
            if provider is None:
                return None
            return {
                "api_key": mask_api_key(provider.api_key),
                "model": provider.model,
                "base_url": provider.base_url,
            }

        return {
            "provider": _mask_provider(self.provider),
            "text": _mask_provider(self.text),
            "vision": _mask_provider(self.vision),
        }


def parse_llm_config(value: Any) -> LLMConfig | None:
    """Parse a raw mapping / LLMConfig into a validated LLMConfig, or None."""
    if value is None:
        return None
    if isinstance(value, LLMConfig):
        return value
    if isinstance(value, dict):
        return LLMConfig.model_validate(value)
    return None
