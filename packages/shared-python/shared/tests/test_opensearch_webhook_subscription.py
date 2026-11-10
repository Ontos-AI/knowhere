"""Subscription omission and exact event lists have distinct meanings."""

import pytest
import os
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def configureImportEnvironment(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults: dict[str, str] = {"S3_BUCKET_NAME": "unit-test", "S3_TEMP_PATH": "/tmp/knowhere-slice-unit",
        "TMP_PATH": "/tmp/knowhere-slice-unit", "DATABASE_URL": "postgresql+asyncpg://unit@127.0.0.1:1/unit"}
    for name, value in defaults.items():
        if name not in os.environ:
            monkeypatch.setenv(name, value)


def test_webhook_events_normalize_duplicates_without_adding_terminal_events() -> None:
    from shared.models.schemas.job import WebhookConfig
    configured = WebhookConfig.model_validate({"url": "https://example.invalid/callback",
        "events": ["job.parsed", "job.parsed"]})
    assert configured.events == ["job.parsed"]
    assert WebhookConfig(url="https://example.invalid/callback").events is None


@pytest.mark.parametrize("events", [None, [], "job.parsed", ["job.PARSED"], ["job.parsed", "unknown"], {"job.parsed": True}])
def test_invalid_explicit_webhook_events_are_rejected(events: object) -> None:
    from shared.models.schemas.job import WebhookConfig
    with pytest.raises(ValidationError):
        WebhookConfig.model_validate({"url": "https://example.invalid/callback", "events": events})
