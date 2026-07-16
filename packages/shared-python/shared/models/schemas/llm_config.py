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


class LLMModelsConfig(BaseModel):
    """Per-channel model ids that share root api_key / base_url."""

    text: Optional[str] = Field(None, min_length=1, description="Text / planning model id")
    vision: Optional[str] = Field(None, min_length=1, description="Vision / VLM model id")


class LLMConfig(BaseModel):
    """OpenAI-compatible BYOK credentials (flat root + optional channel overrides).

    Happy path (one multimodal model for both channels)::

        {"api_key": "...", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"}

    Same endpoint, different models per channel::

        {
          "api_key": "...",
          "base_url": "https://api.openai.com/v1",
          "models": {"text": "gpt-4o-mini", "vision": "gpt-4o"}
        }

    Different endpoints per channel::

        {
          "text": {"api_key": "...", "model": "...", "base_url": "..."},
          "vision": {"api_key": "...", "model": "...", "base_url": "..."}
        }

    Semantics:
    - root ``api_key`` + ``base_url`` with ``model`` and/or ``models`` -> shared auth
    - ``models.<channel>`` wins over root ``model`` for that channel
    - ``text`` / ``vision`` objects fully replace the root for that channel
    - a channel with no resolved model keeps server defaults
    """

    api_key: Optional[str] = Field(None, min_length=1, description="Default provider API key")
    model: Optional[str] = Field(
        None,
        min_length=1,
        description="Default model for both channels (overridden by models.*)",
    )
    base_url: Optional[str] = Field(
        None,
        min_length=1,
        description="Default OpenAI-compatible base URL",
    )
    models: Optional[LLMModelsConfig] = Field(
        None,
        description="Per-channel model ids sharing root api_key / base_url",
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
        has_api_key = self.api_key is not None
        has_base_url = self.base_url is not None
        if has_api_key != has_base_url:
            raise ValueError("llm_config api_key and base_url must be set together")

        has_auth = has_api_key and has_base_url
        has_models = self.models is not None and (
            self.models.text is not None or self.models.vision is not None
        )
        if self.models is not None and not has_models:
            raise ValueError("llm_config.models requires at least one of text or vision")
        if self.model is not None and not has_auth:
            raise ValueError("llm_config.model requires api_key and base_url")
        if has_models and not has_auth:
            raise ValueError("llm_config.models requires api_key and base_url")
        if has_auth and self.model is None and not has_models:
            raise ValueError(
                "llm_config with api_key/base_url requires model and/or models"
            )

        if (
            not has_auth
            and self.text is None
            and self.vision is None
        ):
            raise ValueError(
                "llm_config requires root credentials and/or text/vision overrides"
            )
        return self

    def _channel_model(self, channel: str) -> str | None:
        if self.models is not None:
            named = getattr(self.models, channel)
            if isinstance(named, str) and named:
                return named
        return self.model

    def _root_provider_for(self, channel: str) -> LLMProviderConfig | None:
        if self.api_key is None or self.base_url is None:
            return None
        model = self._channel_model(channel)
        if model is None:
            return None
        return LLMProviderConfig(
            api_key=self.api_key,
            model=model,
            base_url=self.base_url,
        )

    def text_effective(self) -> LLMProviderConfig | None:
        """Return the text-channel config, or None to keep server defaults."""
        return self.text if self.text is not None else self._root_provider_for("text")

    def vision_effective(self) -> LLMProviderConfig | None:
        """Return the vision-channel config, or None to keep server defaults."""
        return (
            self.vision if self.vision is not None else self._root_provider_for("vision")
        )

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
            "api_key": mask_api_key(self.api_key) if self.api_key else None,
            "model": self.model,
            "base_url": self.base_url,
            "models": self.models.model_dump() if self.models is not None else None,
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
