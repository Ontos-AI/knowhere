"""Cold and expired ECS role signing never enters botocore's sync providers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-presign-unit")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import botocore.session
import httpx
import pytest

from shared.core.config.storage import StorageConfig
from shared.core.exceptions.domain_exceptions import UnavailableException
from shared.services.retrieval.lexical.deadline import IndexingDeadline


@pytest.fixture
def roleEnvironment(monkeypatch: pytest.MonkeyPatch) -> None:
    import shared.services.storage.async_presigner as presigner

    presigner._credentialCache.clear()
    for name in tuple(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv(
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/credentials/test-role"
    )

    def refuseDefaultCredentials(self: object) -> None:
        raise AssertionError("Entered synchronous botocore credential provider chain")

    monkeypatch.setattr(
        botocore.session.Session, "get_credentials", refuseDefaultCredentials
    )


def createConfig() -> StorageConfig:
    return StorageConfig(
        S3_TYPE="s3",
        S3_BUCKET_NAME="inputs",
        S3_TEMP_PATH="/tmp",
        S3_REGION="us-east-1",
        S3_ADDRESSING_STYLE="path",
    )


def installMetadata(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    import shared.services.storage.async_presigner as presigner

    originalClient = httpx.AsyncClient

    def createClient(**options: object) -> httpx.AsyncClient:
        return originalClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )

    monkeypatch.setattr(presigner.httpx, "AsyncClient", createClient)


@pytest.mark.asyncio
async def test_cold_role_presigning_uses_async_metadata_and_immutable_credentials(
    roleEnvironment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.services.storage.async_presigner import AsyncStoragePresigner

    calls: list[str] = []

    async def metadata(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        await asyncio.sleep(0)
        return httpx.Response(
            200,
            json={
                "AccessKeyId": "SYNTHETIC-ROLE-KEY",
                "SecretAccessKey": "synthetic-secret",
                "Token": "synthetic-session-token",
                "Expiration": (
                    datetime.now(timezone.utc) + timedelta(hours=6)
                ).isoformat(),
            },
        )

    installMetadata(monkeypatch, metadata)
    signer = AsyncStoragePresigner(config=createConfig())
    url = await signer.generateUrl(
        "retained/revision.zlib",
        method="PUT",
        headers={"Content-Type": "application/zlib"},
        deadline=IndexingDeadline(3),
    )
    parameters = parse_qs(urlsplit(url).query)
    assert calls == ["http://169.254.170.2/v2/credentials/test-role"]
    assert parameters["X-Amz-Credential"][0].startswith("SYNTHETIC-ROLE-KEY/")
    assert parameters["X-Amz-Security-Token"] == ["synthetic-session-token"]
    assert parameters["X-Amz-SignedHeaders"] == ["content-type;host"]
    second = await signer.generateUrl(
        "retained/revision.zlib", deadline=IndexingDeadline(3)
    )
    assert "X-Amz-Signature=" in second and len(calls) == 1


@pytest.mark.asyncio
async def test_role_refresh_wait_is_cancellable_and_uses_original_budget(
    roleEnvironment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.services.storage.async_presigner import AsyncStoragePresigner

    entered: asyncio.Event = asyncio.Event()
    exited: asyncio.Event = asyncio.Event()

    async def metadata(request: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            exited.set()
        return httpx.Response(500)

    installMetadata(monkeypatch, metadata)
    signer = AsyncStoragePresigner(config=createConfig())
    with pytest.raises(TimeoutError):
        await signer.generateUrl("retained/input", deadline=IndexingDeadline(0.02))
    assert entered.is_set() and exited.is_set()


@pytest.mark.asyncio
async def test_expired_role_credentials_refresh_asynchronously(
    roleEnvironment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.services.storage.async_presigner import AsyncStoragePresigner

    calls: list[int] = []

    async def metadata(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "AccessKeyId": f"ROLE-{len(calls)}",
                "SecretAccessKey": "synthetic-secret",
                "Token": "synthetic-token",
                "Expiration": (
                    datetime.now(timezone.utc) + timedelta(hours=6)
                ).isoformat(),
            },
        )

    installMetadata(monkeypatch, metadata)
    signer = AsyncStoragePresigner(config=createConfig())
    await signer.generateUrl("retained/input", deadline=IndexingDeadline(3))
    monkeypatch.setattr(
        signer,
        "_getCurrentTime",
        lambda: datetime.now(timezone.utc) + timedelta(hours=7),
    )
    with pytest.raises(UnavailableException):
        await signer.generateUrl("retained/input", deadline=IndexingDeadline(3))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_static_signing_does_not_enter_default_provider_or_metadata(
    roleEnvironment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.services.storage.async_presigner import AsyncStoragePresigner

    def refuseMetadata(**options: object) -> None:
        raise AssertionError("Static signing made a metadata client")

    import shared.services.storage.async_presigner as presigner

    monkeypatch.setattr(presigner.httpx, "AsyncClient", refuseMetadata)
    config = createConfig().model_copy(
        update={
            "S3_ACCESS_KEY_ID": "STATIC-KEY",
            "S3_SECRET_ACCESS_KEY": "synthetic-static-secret",
            "S3_ENDPOINT_URL": "http://minio.invalid:9000",
            "S3_USE_SSL": False,
        }
    )
    url = await AsyncStoragePresigner(config=config).generateUrl(
        "retained/input", deadline=IndexingDeadline(3)
    )
    assert urlsplit(url).netloc == "minio.invalid:9000"
    assert parse_qs(urlsplit(url).query)["X-Amz-Credential"][0].startswith(
        "STATIC-KEY/"
    )


@pytest.mark.asyncio
async def test_unsupported_provider_fails_closed_without_sync_fallback(
    roleEnvironment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.services.storage.async_presigner import AsyncStoragePresigner

    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
    monkeypatch.setenv("AWS_PROFILE", "unsupported-refresh-profile")
    with pytest.raises(UnavailableException, match="credential"):
        await AsyncStoragePresigner(config=createConfig()).generateUrl(
            "retained/input", deadline=IndexingDeadline(3)
        )


@pytest.mark.asyncio
async def test_unsafe_metadata_target_is_rejected_without_http(
    roleEnvironment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.services.storage.async_presigner import AsyncStoragePresigner

    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
    monkeypatch.setenv(
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "http://public.example.invalid/credentials",
    )
    with pytest.raises(UnavailableException, match="metadata"):
        await AsyncStoragePresigner(config=createConfig()).generateUrl(
            "retained/input", deadline=IndexingDeadline(3)
        )


@pytest.mark.asyncio
async def test_async_result_delivery_avoids_sync_storage_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.jobs.result_delivery import JobResultDeliveryResolver
    from shared.services.storage.async_presigner import AsyncStoragePresigner
    import shared.services.storage.job_file_storage as files

    def refuseFactory() -> None:
        raise AssertionError(
            "Async result delivery constructed synchronous role adapter"
        )

    calls: list[str] = []
    expiresAt: datetime = datetime.now(timezone.utc) + timedelta(minutes=10)

    async def generateUrl(self: object, key: str, **options: object) -> SimpleNamespace:
        calls.append(key)
        return SimpleNamespace(
            url="https://synthetic.invalid/result.zip",
            expiresIn=600,
            expiresAt=expiresAt,
        )

    monkeypatch.setattr(files, "get_cached_storage_adapter", refuseFactory)
    monkeypatch.setattr(AsyncStoragePresigner, "generateUrlWithExpiry", generateUrl)
    result = await JobResultDeliveryResolver().resolveAsync(
        SimpleNamespace(
            inline_payload={"chunks": []}, result_s3_key="retained/result.zip"
        )
    )
    assert calls == ["retained/result.zip"]
    assert result.result_url == "https://synthetic.invalid/result.zip"
    assert result.result_url_expires_at == expiresAt


@pytest.mark.asyncio
async def test_known_role_expiry_bounds_presigned_url_and_advertisement(
    roleEnvironment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.services.storage.async_presigner import AsyncStoragePresigner

    expiresAt = datetime.now(timezone.utc) + timedelta(minutes=10)

    async def metadata(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "AccessKeyId": "SHORT-ROLE",
                "SecretAccessKey": "synthetic-secret",
                "Token": "synthetic-token",
                "Expiration": expiresAt.isoformat(),
            },
        )

    installMetadata(monkeypatch, metadata)
    result = await AsyncStoragePresigner(config=createConfig()).generateUrlWithExpiry(
        "retained/input", deadline=IndexingDeadline(3)
    )
    assert 530 <= result.expiresIn <= 570
    assert result.expiresAt <= expiresAt - timedelta(seconds=29)
    assert (
        int(parse_qs(urlsplit(result.url).query)["X-Amz-Expires"][0])
        == result.expiresIn
    )


@pytest.mark.asyncio
async def test_environment_session_token_requires_known_expiration(
    roleEnvironment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.services.storage.async_presigner import AsyncStoragePresigner

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TEMPORARY-KEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "synthetic-session-token")
    with pytest.raises(UnavailableException, match="expiration"):
        await AsyncStoragePresigner(config=createConfig()).generateUrl(
            "retained/input", deadline=IndexingDeadline(3)
        )


@pytest.mark.asyncio
async def test_environment_session_expiry_bounds_url(
    roleEnvironment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.services.storage.async_presigner import AsyncStoragePresigner

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TEMPORARY-KEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "synthetic-session-token")
    monkeypatch.setenv(
        "AWS_CREDENTIAL_EXPIRATION",
        (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    )
    result = await AsyncStoragePresigner(config=createConfig()).generateUrlWithExpiry(
        "retained/input", deadline=IndexingDeadline(3)
    )
    assert 530 <= result.expiresIn <= 570


@pytest.mark.asyncio
async def test_local_signer_reuses_one_immutable_generation_and_closes_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock
    import shared.services.storage.async_presigner as presigner

    clients: list[MagicMock] = []

    def createClient(*arguments: object, **options: object) -> MagicMock:
        assert options.get("aws_access_key_id") in ("STATIC-ONE", "STATIC-TWO")
        assert options.get("aws_secret_access_key") == "synthetic-secret"
        client: MagicMock = MagicMock()
        client.generate_presigned_url.return_value = "https://synthetic.invalid/signed"
        clients.append(client)
        return client

    session: MagicMock = MagicMock()
    session.client.side_effect = createClient
    monkeypatch.setattr(presigner.boto3.session, "Session", lambda: session)
    config = createConfig().model_copy(
        update={
            "S3_ACCESS_KEY_ID": "STATIC-ONE",
            "S3_SECRET_ACCESS_KEY": "synthetic-secret",
        }
    )
    signer = presigner.AsyncStoragePresigner(config=config)
    for _ in range(10):
        await signer.generateUrl("retained/input")
    assert len(clients) == 1
    clients[0].close.assert_not_called()
    signer.config = config.model_copy(update={"S3_ACCESS_KEY_ID": "STATIC-TWO"})
    await signer.generateUrl("retained/input")
    assert len(clients) == 2
    clients[0].close.assert_called_once()
    clients[1].close.assert_not_called()
    presigner.AsyncStoragePresigner.closeCurrent()
    clients[1].close.assert_called_once()


@pytest.mark.asyncio
async def test_local_signer_configuration_cache_is_bounded_and_closes_evictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock
    import shared.services.storage.async_presigner as presigner

    clients: list[MagicMock] = []

    def createClient(*arguments: object, **options: object) -> MagicMock:
        client: MagicMock = MagicMock()
        client.generate_presigned_url.return_value = "https://synthetic.invalid/signed"
        clients.append(client)
        return client

    session: MagicMock = MagicMock()
    session.client.side_effect = createClient
    monkeypatch.setattr(presigner.boto3.session, "Session", lambda: session)
    config = createConfig().model_copy(
        update={
            "S3_ACCESS_KEY_ID": "STATIC",
            "S3_SECRET_ACCESS_KEY": "synthetic-secret",
        }
    )
    for index in range(12):
        signer = presigner.AsyncStoragePresigner(
            config=config.model_copy(
                update={"S3_ENDPOINT_URL": f"https://synthetic-{index}.invalid"}
            )
        )
        await signer.generateUrl("retained/input")
    assert len(clients) == 12
    assert sum(client.close.call_count == 0 for client in clients) <= 8
    presigner.AsyncStoragePresigner.closeCurrent()
    assert all(client.close.call_count == 1 for client in clients)
