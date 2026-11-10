"""Private native transport credentials without importing application settings.

Each pool lookup reads the mounted files so an atomic secret/certificate rotation
selects a new pool. Previously issued requests retain their old pool until normal
process/loop shutdown; credentials are never embedded in URLs or representations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
import os
from pathlib import Path
import socket
import ssl
import stat
from threading import RLock
from typing import ClassVar
from urllib.parse import urlsplit

import httpx

_PRIVATE_NETWORKS = (
    ip_network("10.0.0.0/8"), ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"), ip_network("fc00::/7"),
)
_MAXIMUM_CREDENTIAL_BYTES: int = 64 * 1024


def _readFile(name: str, *, secret: bool = False) -> bytes | None:
    configured: str | None = os.environ.get(name)
    if not configured:
        return None
    path: Path = Path(configured)
    try:
        metadata: os.stat_result = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAXIMUM_CREDENTIAL_BYTES:
            raise ValueError("Native client credential file is not a bounded regular file")
        if secret and metadata.st_mode & 0o077:
            raise ValueError("Native client secret file permissions must exclude group and other access")
        return path.read_bytes()
    except OSError:
        raise ValueError("Native client credential file cannot be read") from None


def _resolveAddresses(hostname: str, port: int) -> tuple[IPv4Address | IPv6Address, ...]:
    try:
        return (ip_address(hostname),)
    except ValueError:
        try:
            addresses: tuple[IPv4Address | IPv6Address, ...] = tuple({
                ip_address(record[4][0])
                for record in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            })
        except OSError:
            raise ValueError("Native endpoint private DNS cannot be resolved") from None
        if not addresses:
            raise ValueError("Native endpoint private DNS returned no addresses")
        return addresses


@dataclass(frozen=True, repr=False)
class NativeClientConfig:
    _contexts: ClassVar[dict[str, ssl.SSLContext]] = {}
    _contextLock: ClassVar[RLock] = RLock()
    endpoint: str
    identity: str
    verify: ssl.SSLContext | bool = field(repr=False)
    auth: httpx.BasicAuth | None = field(repr=False)

    @classmethod
    def resolve(cls, endpoint: str, *, environmentPrefix: str = "KNOWHERE_OPENSEARCH_") -> NativeClientConfig:
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ValueError("Native endpoint must be an origin URL without credentials or query data")
        addresses: tuple[IPv4Address | IPv6Address, ...] = _resolveAddresses(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
        if any(not address.is_loopback and not any(address in network for network in _PRIVATE_NETWORKS)
               for address in addresses):
            raise ValueError("Native endpoint must resolve exclusively to private network addresses")
        isLoopback: bool = all(address.is_loopback for address in addresses)
        if parsed.scheme == "http":
            if not isLoopback:
                raise ValueError("Private native endpoints require HTTPS")
            return cls(endpoint.rstrip("/"), "loopback-component-test", True, None)
        authority: bytes | None = _readFile(environmentPrefix + "CA_FILE")
        if authority is None:
            raise ValueError("Native HTTPS requires an explicit CA file")
        certificate: bytes | None = _readFile(environmentPrefix + "CLIENT_CERT_FILE")
        privateKey: bytes | None = _readFile(environmentPrefix + "CLIENT_KEY_FILE", secret=True)
        if (certificate is None) != (privateKey is None):
            raise ValueError("Native mutual TLS requires both certificate and key")
        username: str | None = os.environ.get(environmentPrefix + "USERNAME")
        password: bytes | None = _readFile(environmentPrefix + "PASSWORD_FILE", secret=True)
        if (username is None) != (password is None):
            raise ValueError("Native basic authentication requires username and password file")
        if certificate is None and password is None:
            raise ValueError("Native HTTPS requires service authentication")
        auth: httpx.BasicAuth | None = None
        if username is not None and password is not None:
            try:
                secret: str = password.decode("utf-8").rstrip("\r\n")
            except UnicodeError:
                raise ValueError("Native password file must contain UTF-8 text") from None
            if not username.strip() or not secret:
                raise ValueError("Native basic authentication must not contain empty credentials")
            auth = httpx.BasicAuth(username, secret)
        digest = sha256()
        for component in (authority, certificate or b"", privateKey or b"", (username or "").encode(), password or b""):
            digest.update(len(component).to_bytes(8, "big"))
            digest.update(component)
        identity: str = digest.hexdigest()
        with cls._contextLock:
            context: ssl.SSLContext | None = cls._contexts.get(identity)
            if context is None:
                try:
                    context = ssl.create_default_context(cadata=authority.decode("utf-8"))
                    if certificate is not None:
                        context.load_cert_chain(
                            os.environ[environmentPrefix + "CLIENT_CERT_FILE"],
                            os.environ[environmentPrefix + "CLIENT_KEY_FILE"],
                        )
                except (OSError, ValueError, ssl.SSLError):
                    raise ValueError("Native client CA or certificate/key configuration is invalid") from None
                if len(cls._contexts) >= 8:
                    cls._contexts.pop(next(iter(cls._contexts)))
                cls._contexts[identity] = context
        return cls(endpoint.rstrip("/"), identity, context, auth)
