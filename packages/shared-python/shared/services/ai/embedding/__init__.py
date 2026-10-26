"""Multimodal embedding client package."""

from shared.services.ai.embedding.multimodal_client import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    embed_images,
    embed_query,
    resolve_embedding_model,
)

__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "DEFAULT_EMBEDDING_MODEL",
    "embed_images",
    "embed_query",
    "resolve_embedding_model",
]
