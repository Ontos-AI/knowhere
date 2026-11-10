"""Local URL signing with bounded async ECS credentials, never a sync provider.

The API supports explicit keys, AWS environment keys and the ECS container role
endpoint. Profile/process/web-identity/IMDS providers and authorization-token
files fail closed here: their synchronous SDK refreshers cannot honor an async
request deadline. Sync workers retain the ordinary storage adapter/provider chain.
Temporary environment sessions require AWS_CREDENTIAL_EXPIRATION so public URL
expiry can be bounded by the credential lifetime. Configured static keys retain
their requested URL lifetime.
No cloud object request is made by this module.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from ipaddress import ip_address
import os
import time
from typing import Protocol
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import boto3.session
from botocore.client import BaseClient
from botocore.config import Config
import httpx

from shared.core.config import settings
from shared.core.config.storage import StorageConfig
from shared.core.exceptions.domain_exceptions import UnavailableException


class _Deadline(Protocol):
    def getRemainingSeconds(self) -> float: ...


class _SigningDeadline:
    def __init__(self, seconds: float) -> None:
        self.expiresAt: float = time.monotonic() + seconds

    def getRemainingSeconds(self) -> float:
        remaining: float = self.expiresAt - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Storage signing deadline exceeded")
        return remaining


@dataclass(frozen=True, repr=False)
class _Credentials:
    accessKey: str = field(repr=False)
    secretKey: str = field(repr=False)
    token: str | None = field(repr=False)
    expiresAt: datetime | None = None


@dataclass(frozen=True)
class _PresignedURL:
    url: str
    expiresIn: int
    expiresAt: datetime


@dataclass(frozen=True, repr=False)
class _CachedSigner:
    client: BaseClient
    credentialIdentity: str
    processId: int


_credentialCache: dict[str, _Credentials] = {}
_signerCaches: WeakKeyDictionary[
    asyncio.AbstractEventLoop, OrderedDict[str, _CachedSigner]
] = WeakKeyDictionary()
_MAXIMUM_SIGNING_CONFIGURATIONS: int = 8
_refreshLocks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    WeakKeyDictionary()
)
_ALLOWED_METADATA_HOSTS: frozenset[str] = frozenset(
    {"169.254.170.2", "169.254.170.23", "fd00:ec2::23", "localhost"}
)


class AsyncStoragePresigner:
    """Sign only GET/PUT URLs; credentials are immutable for the entire signature."""

    def __init__(self, *, config: StorageConfig | None = None) -> None:
        self.config: StorageConfig = config if config is not None else settings

    @staticmethod
    def closeCurrent() -> None:
        clients: OrderedDict[str, _CachedSigner] = _signerCaches.pop(
            asyncio.get_running_loop(), OrderedDict()
        )
        for cached in clients.values():
            cached.client.close()
        _credentialCache.clear()

    async def generateUrl(
        self,
        key: str,
        *,
        deadline: _Deadline | None = None,
        bucket: str | None = None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        expiration: int = 3600,
    ) -> str:
        result: _PresignedURL = await self.generateUrlWithExpiry(
            key,
            deadline=deadline,
            bucket=bucket,
            method=method,
            headers=headers,
            expiration=expiration,
        )
        return result.url

    async def generateUrlWithExpiry(
        self,
        key: str,
        *,
        deadline: _Deadline | None = None,
        bucket: str | None = None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        expiration: int = 3600,
    ) -> _PresignedURL:
        if method not in ("GET", "PUT") or not key or not 1 <= expiration <= 604800:
            raise ValueError(
                "Presigning requires a key, GET/PUT method and a supported expiration"
            )
        deadline = deadline if deadline is not None else _SigningDeadline(5)
        async with asyncio.timeout(deadline.getRemainingSeconds()):
            signedAt: datetime = self._getCurrentTime().replace(microsecond=0)
            if self.config.S3_TYPE == "filesystem":
                # Contract/local backend only; no role provider or cloud I/O.
                adapter = self.config.get_storage_adapter()
                localURL: str = adapter.generate_presigned_url(
                    key,
                    expiration=expiration,
                    bucket=bucket,
                    method=method,
                    headers=headers,
                )
                return _PresignedURL(
                    localURL, expiration, signedAt + timedelta(seconds=expiration)
                )
            if self.config.S3_TYPE == "oss":
                # get_oss_bucket constructs the configured static Auth, with no I/O.
                if (
                    not self.config.S3_ACCESS_KEY_ID
                    or not self.config.S3_SECRET_ACCESS_KEY
                ):
                    raise self._unavailable(
                        "Async OSS signing requires explicit credentials"
                    )
                adapter = self.config.get_storage_adapter()
                objectURL: str = adapter.generate_presigned_url(
                    key,
                    expiration=expiration,
                    bucket=bucket,
                    method=method,
                    headers=headers,
                )
                return _PresignedURL(
                    objectURL, expiration, signedAt + timedelta(seconds=expiration)
                )
            credentials: _Credentials = await self._getCredentials(deadline, expiration)
            deadline.getRemainingSeconds()
            signedAt = self._getCurrentTime().replace(microsecond=0)
            effectiveExpiration: int = expiration
            if credentials.expiresAt is not None:
                effectiveExpiration = min(
                    expiration,
                    int(
                        (credentials.expiresAt - self._getCurrentTime()).total_seconds()
                    )
                    - 30,
                )
                if effectiveExpiration < 1:
                    raise self._unavailable(
                        "Storage credential expiration leaves no usable signing lifetime"
                    )
            result: str = self._sign(
                key,
                bucket or self.config.S3_BUCKET_NAME,
                method,
                headers,
                effectiveExpiration,
                credentials,
            )
            deadline.getRemainingSeconds()
            return _PresignedURL(
                result,
                effectiveExpiration,
                signedAt + timedelta(seconds=effectiveExpiration),
            )

    async def _getCredentials(
        self, deadline: _Deadline, expiration: int
    ) -> _Credentials:
        accessKey: str = self.config.S3_ACCESS_KEY_ID or os.environ.get(
            "AWS_ACCESS_KEY_ID", ""
        )
        secretKey: str = self.config.S3_SECRET_ACCESS_KEY or os.environ.get(
            "AWS_SECRET_ACCESS_KEY", ""
        )
        if accessKey or secretKey:
            if not accessKey or not secretKey:
                raise self._unavailable(
                    "Async storage credentials require both explicit keys"
                )
            token: str | None = (
                None
                if self.config.S3_ACCESS_KEY_ID
                else os.environ.get("AWS_SESSION_TOKEN")
                or os.environ.get("AWS_SECURITY_TOKEN")
            )
            expiry: str = (
                os.environ.get("AWS_CREDENTIAL_EXPIRATION", "")
                if not self.config.S3_ACCESS_KEY_ID
                else ""
            )
            expiresAt: datetime | None = None
            if token and not expiry:
                raise self._unavailable(
                    "Async session credentials require a known AWS_CREDENTIAL_EXPIRATION expiration"
                )
            if expiry:
                try:
                    expiresAt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                    if (
                        expiresAt.tzinfo is None
                        or expiresAt <= self._getCurrentTime() + timedelta(seconds=30)
                    ):
                        raise ValueError("Expired or unqualified credentials")
                except ValueError:
                    raise self._unavailable(
                        "Async environment credential expiration is invalid or expired"
                    ) from None
            return _Credentials(accessKey, secretKey, token, expiresAt)
        if any(
            os.environ.get(name)
            for name in (
                "AWS_PROFILE",
                "AWS_DEFAULT_PROFILE",
                "AWS_WEB_IDENTITY_TOKEN_FILE",
                "AWS_ROLE_ARN",
            )
        ):
            raise self._unavailable(
                "Async storage credential profile and web-identity providers require separate qualification"
            )
        url, headers = self._getMetadataRequest()
        cacheKey: str = sha256(
            (url + "\n" + headers.get("Authorization", "")).encode()
        ).hexdigest()
        cached: _Credentials | None = _credentialCache.get(cacheKey)
        if self._canUse(cached, expiration):
            assert cached is not None
            return cached
        loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        locks: dict[str, asyncio.Lock] = _refreshLocks.setdefault(loop, {})
        lock: asyncio.Lock = locks.setdefault(cacheKey, asyncio.Lock())
        async with lock:
            deadline.getRemainingSeconds()
            cached = _credentialCache.get(cacheKey)
            if self._canUse(cached, expiration):
                assert cached is not None
                return cached
            credentials: _Credentials = await self._fetchCredentials(
                url, headers, deadline
            )
            if not self._canUse(credentials, 1):
                raise self._unavailable(
                    "ECS metadata returned expired storage credentials"
                )
            if len(_credentialCache) >= 16:
                _credentialCache.clear()
            _credentialCache[cacheKey] = credentials
            return credentials

    def _canUse(self, credentials: _Credentials | None, expiration: int) -> bool:
        return (
            credentials is not None
            and credentials.expiresAt is not None
            and (credentials.expiresAt - self._getCurrentTime()).total_seconds()
            > min(expiration, 300) + 30
        )

    @staticmethod
    def _getCurrentTime() -> datetime:
        return datetime.now(timezone.utc)

    def _getMetadataRequest(self) -> tuple[str, dict[str, str]]:
        relative: str = os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "")
        full: str = os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI", "")
        if relative:
            if not relative.startswith("/") or relative.startswith("//"):
                raise self._unavailable("ECS metadata relative URI is invalid")
            url: str = f"http://169.254.170.2{relative}"
        elif full:
            url = full
        else:
            raise self._unavailable(
                "Async storage credential provider is unsupported; configure explicit keys or an ECS container role"
            )
        try:
            parsed = urlsplit(url)
            parsed.port
        except ValueError:
            raise self._unavailable("ECS metadata URI is invalid") from None
        try:
            isLoopback: bool = (
                parsed.hostname is not None and ip_address(parsed.hostname).is_loopback
            )
        except ValueError:
            isLoopback = False
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not (isLoopback or parsed.hostname in _ALLOWED_METADATA_HOSTS)
        ):
            raise self._unavailable(
                "ECS metadata URI must target a permitted container or loopback host"
            )
        if os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"):
            raise self._unavailable(
                "Async storage credential token-file providers require separate qualification"
            )
        token: str = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN", "")
        if "\r" in token or "\n" in token:
            raise self._unavailable("ECS metadata authorization token is invalid")
        headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = token
        return url, headers

    async def _fetchCredentials(
        self, url: str, headers: dict[str, str], deadline: _Deadline
    ) -> _Credentials:
        try:
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False
            ) as client:
                async with client.stream(
                    "GET",
                    url,
                    headers=headers,
                    timeout=min(2.0, deadline.getRemainingSeconds()),
                ) as response:
                    if response.status_code != 200:
                        raise self._unavailable(
                            "ECS metadata credentials are unavailable"
                        )
                    chunks: list[bytes] = []
                    size: int = 0
                    async for chunk in response.aiter_bytes():
                        deadline.getRemainingSeconds()
                        size += len(chunk)
                        if size > 65536:
                            raise self._unavailable(
                                "ECS metadata credential response exceeds its limit"
                            )
                        chunks.append(chunk)
            import json

            payload: object = json.loads(b"".join(chunks))
            if not isinstance(payload, dict):
                raise ValueError("Invalid credential payload")
            values: list[object] = [
                payload.get(name)
                for name in ("AccessKeyId", "SecretAccessKey", "Token", "Expiration")
            ]
            if not all(isinstance(value, str) and value for value in values):
                raise ValueError("Incomplete credential payload")
            accessKey, secretKey, token, expiry = (str(value) for value in values)
            expiresAt: datetime = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if expiresAt.tzinfo is None:
                raise ValueError("Credential expiration requires a timezone")
            return _Credentials(accessKey, secretKey, token, expiresAt)
        except (httpx.TimeoutException, TimeoutError):
            raise TimeoutError(
                "ECS credential retrieval exceeded its bounded timeout"
            ) from None
        except (httpx.HTTPError, ValueError, TypeError):
            # Never put credential response bodies, authorization headers or URLs
            # into an exception that may be retained on a public Job snapshot.
            raise self._unavailable(
                "ECS metadata returned invalid or unavailable storage credentials"
            ) from None

    def _sign(
        self,
        key: str,
        bucket: str,
        method: str,
        headers: dict[str, str] | None,
        expiration: int,
        credentials: _Credentials,
    ) -> str:
        if bucket.startswith("arn:") or bucket.endswith("--x-s3"):
            raise self._unavailable(
                "Async storage signing supports ordinary S3 buckets only"
            )
        client: BaseClient = self._getSigningClient(credentials)
        parameters: dict[str, str] = {"Bucket": bucket, "Key": key}
        if headers:
            contentType: str | None = headers.get("Content-Type") or headers.get(
                "content-type"
            )
            if contentType:
                parameters[
                    "ContentType" if method == "PUT" else "ResponseContentType"
                ] = contentType
            if method == "GET" and headers.get("Content-Disposition"):
                parameters["ResponseContentDisposition"] = headers[
                    "Content-Disposition"
                ]
        return client.generate_presigned_url(
            "put_object" if method == "PUT" else "get_object",
            Params=parameters,
            ExpiresIn=expiration,
        )

    def _getSigningClient(self, credentials: _Credentials) -> BaseClient:
        region: str = (
            self.config.S3_REGION
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        configurationIdentity: str = sha256(
            repr(
                (
                    self.config.S3_ENDPOINT_URL,
                    region,
                    self.config.S3_USE_SSL,
                    self.config.S3_ADDRESSING_STYLE,
                    tuple(
                        os.environ.get(name)
                        for name in (
                            "AWS_ENDPOINT_URL",
                            "AWS_ENDPOINT_URL_S3",
                            "AWS_USE_FIPS_ENDPOINT",
                            "AWS_USE_DUALSTACK_ENDPOINT",
                        )
                    ),
                )
            ).encode()
        ).hexdigest()
        credentialIdentity: str = sha256(
            repr(
                (credentials.accessKey, credentials.secretKey, credentials.token)
            ).encode()
        ).hexdigest()
        clients: OrderedDict[str, _CachedSigner] = _signerCaches.setdefault(
            asyncio.get_running_loop(), OrderedDict()
        )
        previous: _CachedSigner | None = clients.pop(configurationIdentity, None)
        if previous is not None:
            if (
                previous.processId == os.getpid()
                and previous.credentialIdentity == credentialIdentity
            ):
                clients[configurationIdentity] = previous
                return previous.client
            # No old credential generation survives a rotation for this config.
            previous.client.close()
        # Explicit immutable strings prevent provider discovery/refresh. Client
        # construction and signing are local; no cloud request/pool is used.
        client: BaseClient = boto3.session.Session().client(
            "s3",
            aws_access_key_id=credentials.accessKey,
            aws_secret_access_key=credentials.secretKey,
            aws_session_token=credentials.token,
            region_name=region,
            endpoint_url=self.config.S3_ENDPOINT_URL or None,
            use_ssl=self.config.S3_USE_SSL,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": self.config.S3_ADDRESSING_STYLE},
                retries={"max_attempts": 0},
            ),
        )
        if len(clients) >= _MAXIMUM_SIGNING_CONFIGURATIONS:
            _, retired = clients.popitem(last=False)
            retired.client.close()
        clients[configurationIdentity] = _CachedSigner(
            client, credentialIdentity, os.getpid()
        )
        return client

    @staticmethod
    def _unavailable(message: str) -> UnavailableException:
        return UnavailableException(message, retry_after=1)
