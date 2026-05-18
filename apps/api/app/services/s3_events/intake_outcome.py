from __future__ import annotations

from dataclasses import dataclass


_SENSITIVE_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "x-amz-sns-signature",
        "x-amz-sns-signing-cert-url",
        "x-minio-auth-token",
        "x-oss-signature",
        "x-oss-pub-key-url",
        "x-mns-signature",
        "x-mns-signing-cert-url",
    }
)


@dataclass(frozen=True)
class StorageEventIntakeOutcome:
    message: str
    reason: str

    def to_response(self) -> dict[str, str]:
        return {"message": self.message}


def build_storage_event_error_acknowledgement() -> StorageEventIntakeOutcome:
    return StorageEventIntakeOutcome(
        message="Event handling completed",
        reason="acked_after_handler_error",
    )


def sanitize_storage_event_headers(headers: dict[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in _SENSITIVE_HEADER_NAMES:
            sanitized[name] = "<redacted>"
        else:
            sanitized[name] = value
    return sanitized
