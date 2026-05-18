from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from typing import Any

from loguru import logger


def decode_nested_json_message(inner: str) -> dict[str, Any] | None:
    for decoder in (_decode_base64_json, _decode_plain_json):
        decoded = _try_decode(inner, decoder)
        if decoded is not None:
            return decoded
    return None


def _try_decode(
    inner: str,
    decoder: Callable[[str], object],
) -> dict[str, Any] | None:
    try:
        decoded = decoder(inner)
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        logger.debug("Nested storage event message decode failed: {}", exc)
        return None
    return decoded if isinstance(decoded, dict) else None


def _decode_base64_json(inner: str) -> object:
    decoded_bytes = base64.b64decode(inner, validate=True)
    decoded_str = decoded_bytes.decode("utf-8")
    return json.loads(decoded_str)


def _decode_plain_json(inner: str) -> object:
    return json.loads(inner)
