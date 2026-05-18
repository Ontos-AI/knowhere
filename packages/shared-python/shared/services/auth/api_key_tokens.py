"""API key generation, masking, and hashing helpers."""

from hashlib import sha256
from secrets import token_urlsafe
from typing import TypeGuard

API_KEY_PREFIX: str = "sk_"
API_KEY_RANDOM_BYTES: int = 32


def generate_api_key() -> str:
    """Generate a new plaintext API key with cryptographic randomness."""
    return f"{API_KEY_PREFIX}{token_urlsafe(API_KEY_RANDOM_BYTES)}"


def hash_api_key(api_key: str) -> str:
    """Return a deterministic SHA-256 digest for API key lookup."""
    # API keys are high-entropy bearer tokens; this digest is only a DB lookup key.
    return sha256(api_key.encode("utf-8")).hexdigest()


def mask_api_key(api_key: str) -> str:
    """Mask an API key, exposing only the first 8 and last 4 characters."""
    if len(api_key) < 12:
        return api_key
    return api_key[:8] + "•" * (len(api_key) - 12) + api_key[-4:]


def is_api_key_token(token: object) -> TypeGuard[str]:
    """Return whether a bearer token has the API-key prefix."""
    return isinstance(token, str) and token.startswith(API_KEY_PREFIX)
