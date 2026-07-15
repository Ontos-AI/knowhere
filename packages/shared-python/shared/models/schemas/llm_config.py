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
    """OpenAI-compatible BYOK credentials (flat root + optional channel overrides).

    Happy path (one multimodal model for both channels), matching OpenAI /
    LangChain / LiteLLM style::

        {"api_key": "...", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"}

    Different endpoints per channel::

        {
          "text": {"api_key": "...", "model": "...", "base_url": "..."},
          "vision": {"api_key": "...", "model": "...", "base_url": "..."}
        }

    Semantics:
    - root ``api_key`` / ``model`` / ``base_url`` (all three together) -> default
      for both channels
    - ``text`` / ``vision`` fully replace the default for that channel
    - a channel with neither a slot nor a root default keeps server defaults
    """

    api_key: Optional[str] = Field(None, min_length=1, description="Default provider API key")
    model: Optional[str] = Field(None, min_length=1, description="Default model identifier")
    base_url: Optional[str] = Field(
        None,
        min_length=1,
        description="Default OpenAI-compatible base URL",
    )
    text: Optional[LLMProviderConfig] = Field(
        None,
        description="Text / planning credentials (replaces root for text channel)",
    )
    vision: Optional[LLMProviderConfig] = Field(
        None,
        description="Vision / VLM credentials (replaces root for vision channel)",
    )

    @model_validator(mode="after")
    def _validate_shape(self) -> "LLMConfig":
        root_fields = (self.api_key, self.model, self.base_url)
        root_set_count = sum(value is not None for value in root_fields)
        if root_set_count not in (0, 3):
            raise ValueError(
                "llm_config root api_key, model, and base_url must be set together"
            )
        if root_set_count == 0 and self.text is None and self.vision is None:
            raise ValueError(
                "llm_config requires root credentials and/or text/vision overrides"
            )
        return self

    def root_provider(self) -> LLMProviderConfig | None:
        """Return the flat root as a provider config, or None if unset."""
        if self.api_key is None or self.model is None or self.base_url is None:
            return None
        return LLMProviderConfig(
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
        )

    def text_effective(self) -> LLMProviderConfig | None:
        """Return the text-channel config, or None to keep server defaults."""
        return self.text if self.text is not None else self.root_provider()

    def vision_effective(self) -> LLMProviderConfig | None:
        """Return the vision-channel config, or None to keep server defaults."""
        return self.vision if self.vision is not None else self.root_provider()

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

        dump: dict[str, Any] = {
            "api_key": mask_api_key(self.api_key) if self.api_key else None,
            "model": self.model,
            "base_url": self.base_url,
            "text": _mask_provider(self.text),
            "vision": _mask_provider(self.vision),
        }
        return dump


def parse_llm_config(value: Any) -> LLMConfig | None:
    """Parse a raw mapping / LLMConfig into a validated LLMConfig, or None."""
    if value is None:
        return None
    if isinstance(value, LLMConfig):
        return value
    if isinstance(value, dict):
        return LLMConfig.model_validate(value)
    return None
