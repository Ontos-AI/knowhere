"""Webhook request signing."""

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any


def sign_webhook_payload(payload: Mapping[str, Any], secret: str) -> str:
    """Generate the timestamped Knowhere webhook HMAC signature."""
    timestamp = int(time.time())
    payload_text = json.dumps(payload, separators=(",", ":"))
    signed_content = f"{timestamp}.{payload_text}"
    signature = hmac.new(
        secret.encode("utf-8"),
        signed_content.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return f"t={timestamp},v1={signature}"


def build_webhook_headers(
    *, payload: Mapping[str, Any], secret: str, attempt_id: str
) -> dict[str, str]:
    """Build signed HTTP headers for a direct webhook delivery attempt."""
    return {
        "Content-Type": "application/json",
        "X-Knowhere-Signature": sign_webhook_payload(payload, secret),
        "X-Knowhere-Attempt-ID": attempt_id,
        "User-Agent": "Knowhere-Webhook/1.0",
    }
