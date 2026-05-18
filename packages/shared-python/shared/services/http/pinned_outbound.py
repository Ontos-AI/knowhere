"""Pinned outbound HTTP helpers."""

from __future__ import annotations

import os
import socket
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from urllib3 import Retry
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util import Timeout
from urllib3.util.connection import create_connection

from shared.core.exceptions.domain_exceptions import ValidationException
from shared.services.http.url_security import SafePublicHTTPURL


@dataclass(frozen=True)
class PinnedOutboundResponse:
    """Minimal response metadata for pinned outbound HTTP requests."""

    status: int


@dataclass(frozen=True)
class PinnedDownloadResult:
    """Metadata for a pinned HTTP download to a temporary file."""

    status: int
    temp_file_path: str


class PinnedIPResolver(AbstractResolver):
    """Resolver that always returns the supplied pinned IP address."""

    def __init__(self, pinned_ip: str) -> None:
        self.pinned_ip = pinned_ip

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        pinned_family: int = (
            socket.AF_INET6 if ":" in self.pinned_ip else socket.AF_INET
        )
        return [
            {
                "hostname": host,
                "host": self.pinned_ip,
                "port": port,
                "family": pinned_family,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        pass


class PinnedHTTPConnection(HTTPConnection):
    """HTTP connection that resolves the hostname to a pinned IP address."""

    def __init__(self, *args: Any, pinned_ip: str, **kwargs: Any) -> None:
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def _new_conn(self) -> socket.socket:
        return create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            source_address=self.source_address,
            socket_options=self.socket_options,
        )


class PinnedHTTPSConnection(HTTPSConnection):
    """HTTPS connection that resolves the hostname to a pinned IP address."""

    def __init__(self, *args: Any, pinned_ip: str, **kwargs: Any) -> None:
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def _new_conn(self) -> socket.socket:
        return create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            source_address=self.source_address,
            socket_options=self.socket_options,
        )


class PinnedHTTPConnectionPool(HTTPConnectionPool):
    """HTTP pool that pins DNS resolution to a fixed IP address."""

    ConnectionCls = PinnedHTTPConnection  # pyright: ignore[reportAssignmentType]

    def __init__(self, *args: Any, pinned_ip: str, **kwargs: Any) -> None:
        kwargs["pinned_ip"] = pinned_ip
        super().__init__(*args, **kwargs)


class PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    """HTTPS pool that pins DNS resolution to a fixed IP address."""

    ConnectionCls = PinnedHTTPSConnection  # pyright: ignore[reportAssignmentType]

    def __init__(self, *args: Any, pinned_ip: str, **kwargs: Any) -> None:
        kwargs["pinned_ip"] = pinned_ip
        super().__init__(*args, **kwargs)


def _build_host_header(parsed_url: Any) -> str:
    hostname = parsed_url.hostname
    if not hostname:
        return ""

    if ":" in hostname and not hostname.startswith("["):
        formatted_host = f"[{hostname}]"
    else:
        formatted_host = hostname

    if parsed_url.port is not None:
        return f"{formatted_host}:{parsed_url.port}"
    return formatted_host


def _validate_download_url(url: str, field: str) -> SafePublicHTTPURL:
    parsed_url = urlsplit(url)
    if parsed_url.scheme not in {"http", "https"}:
        raise ValidationException(
            user_message="Invalid URL",
            violations=[
                {"field": field, "description": "URL must use http or https"}
            ],
        )
    if not parsed_url.hostname:
        raise ValidationException(
            user_message="Invalid URL",
            violations=[{"field": field, "description": "URL must include a hostname"}],
        )
    return SafePublicHTTPURL(url)


async def send_pinned_outbound_request(
    *,
    method: str,
    url: str,
    pinned_ip: str,
    timeout_seconds: float,
    headers: Mapping[str, str] | None = None,
    json_body: Any | None = None,
) -> PinnedOutboundResponse:
    """Send an outbound HTTP request through a resolver pinned to a validated IP."""
    validated_url = _validate_download_url(url, "url")
    connector = aiohttp.TCPConnector(resolver=PinnedIPResolver(pinned_ip))
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
    ) as session:
        async with session.request(
            method=method,
            url=validated_url,
            headers=headers,
            json=json_body,
            allow_redirects=False,
        ) as response:
            return PinnedOutboundResponse(status=response.status)


async def download_pinned_outbound_file_async(
    *,
    url: str,
    pinned_ip: str,
    timeout_seconds: float,
    user_agent: str,
    temp_dir: str | None = None,
    field: str = "source_url",
) -> PinnedDownloadResult:
    """Download a file with aiohttp while pinning DNS to a validated IP."""
    validated_url = _validate_download_url(url, field)

    temp_file_descriptor, temp_file_path = tempfile.mkstemp(dir=temp_dir)
    os.close(temp_file_descriptor)

    connector = aiohttp.TCPConnector(resolver=PinnedIPResolver(pinned_ip))
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    try:
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
        ) as session:
            async with session.get(
                validated_url,
                allow_redirects=False,
                headers={"User-Agent": user_agent},
            ) as response:
                if not 200 <= response.status < 300:
                    raise ValidationException(
                        user_message="Invalid URL",
                        violations=[
                            {
                                "field": field,
                                "description": f"URL request failed with status {response.status}",
                            }
                        ],
                    )

                with open(temp_file_path, "wb") as output_file:
                    async for chunk in response.content.iter_chunked(65536):
                        if chunk:
                            output_file.write(chunk)

                return PinnedDownloadResult(
                    status=response.status,
                    temp_file_path=temp_file_path,
                )
    except Exception:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        raise


def download_pinned_outbound_file(
    *,
    url: str,
    pinned_ip: str,
    timeout_seconds: float,
    user_agent: str,
    temp_dir: str | None = None,
    field: str = "source_url",
) -> PinnedDownloadResult:
    """
    Download a file through a resolver pinned to a validated IP.

    Redirects are blocked to prevent redirect-based SSRF. The request connects
    to the pre-validated IP while preserving the original Host header.
    """
    validated_url = _validate_download_url(url, field)
    parsed_url = urlsplit(validated_url)

    temp_file_descriptor, temp_file_path = tempfile.mkstemp(dir=temp_dir)
    os.close(temp_file_descriptor)

    try:
        connection_pool: HTTPConnectionPool
        request_path = parsed_url.path or "/"
        if parsed_url.query:
            request_path = f"{request_path}?{parsed_url.query}"

        if parsed_url.scheme == "https":
            connection_pool = PinnedHTTPSConnectionPool(
                parsed_url.hostname,
                parsed_url.port or 443,
                pinned_ip=pinned_ip,
                retries=Retry(total=0, redirect=False),
            )
        else:
            connection_pool = PinnedHTTPConnectionPool(
                parsed_url.hostname,
                parsed_url.port or 80,
                pinned_ip=pinned_ip,
                retries=Retry(total=0, redirect=False),
            )

        response = connection_pool.urlopen(
            "GET",
            request_path,
            timeout=Timeout.from_float(timeout_seconds),
            preload_content=False,
            redirect=False,
            headers={"User-Agent": user_agent, "Host": _build_host_header(parsed_url)},
        )
        try:
            if not 200 <= response.status < 300:
                raise ValidationException(
                    user_message="Invalid URL",
                    violations=[
                        {
                            "field": field,
                            "description": f"URL request failed with status {response.status}",
                        }
                    ],
                )

            with open(temp_file_path, "wb") as output_file:
                for chunk in response.stream(65536):
                    if chunk:
                        output_file.write(chunk)
        finally:
            response.release_conn()
            response.close()

        return PinnedDownloadResult(
            status=response.status,
            temp_file_path=temp_file_path,
        )
    except Exception:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        raise
