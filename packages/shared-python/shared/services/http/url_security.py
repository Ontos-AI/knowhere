import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Literal, TypeAlias, cast
from urllib.parse import urlparse

SocketAddress: TypeAlias = tuple[object, ...]
AddressInfo: TypeAlias = tuple[int, int, int, str, SocketAddress]
AllowedIPAddress: TypeAlias = ipaddress.IPv4Address | ipaddress.IPv6Address
URLValidationFailureReason: TypeAlias = Literal[
    "unsupported_scheme",
    "missing_hostname",
    "hostname_resolution_failed",
    "invalid_resolved_address",
    "hostname_not_allowed",
    "validation_failed",
]
HTTP_SCHEMES: frozenset[str] = frozenset({"http", "https"})
DEVELOPMENT_ENVIRONMENTS: frozenset[str] = frozenset({"dev", "development", "local"})
BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {"localhost", "ip6-localhost", "ip6-loopback"}
)


class SafePublicHTTPURL(str):
    """A URL string that has passed public HTTP SSRF validation."""


@dataclass(frozen=True)
class HTTPURLValidationResult:
    """Validated HTTP URL and its pinned IP address."""

    is_valid: bool
    url: str
    hostname: str | None = None
    validated_ip: str | None = None
    error_message: str | None = None
    failure_reason: URLValidationFailureReason | None = None


def validate_http_url_and_resolve_ip(
    url: str,
) -> HTTPURLValidationResult:
    """Validate an HTTP URL policy and return the pinned resolved IP address."""
    try:
        can_use_private_hosts = _can_use_private_hosts()
        hostname_result = _validate_url_hostname(url)
        if not hostname_result.is_valid:
            return hostname_result

        hostname = cast(str, hostname_result.hostname)
        if not can_use_private_hosts and _is_blocked_hostname(hostname):
            return _build_url_validation_failure(
                url,
                f"Hostname {hostname} failed validation",
                "hostname_not_allowed",
            )

        try:
            address_infos = cast(list[AddressInfo], socket.getaddrinfo(hostname, None))
        except socket.gaierror as exc:
            return _build_url_validation_failure(
                url,
                f"Unable to resolve hostname {hostname}: {exc}",
                "hostname_resolution_failed",
            )

        return _validate_resolved_addresses(
            url,
            hostname,
            address_infos,
            can_use_private_hosts=can_use_private_hosts,
        )
    except Exception as exc:
        return _build_url_validation_failure(
            url,
            f"URL validation failed: {exc}",
            "validation_failed",
        )


async def validate_http_url_and_resolve_ip_async(
    url: str,
) -> HTTPURLValidationResult:
    """Validate an HTTP URL policy asynchronously and return the pinned IP address."""
    try:
        can_use_private_hosts = _can_use_private_hosts()
        hostname_result = _validate_url_hostname(url)
        if not hostname_result.is_valid:
            return hostname_result

        hostname = cast(str, hostname_result.hostname)
        if not can_use_private_hosts and _is_blocked_hostname(hostname):
            return _build_url_validation_failure(
                url,
                f"Hostname {hostname} failed validation",
                "hostname_not_allowed",
            )

        try:
            loop = asyncio.get_running_loop()
            address_infos = cast(
                list[AddressInfo],
                await loop.getaddrinfo(hostname, None),
            )
        except socket.gaierror as exc:
            return _build_url_validation_failure(
                url,
                f"Unable to resolve hostname {hostname}: {exc}",
                "hostname_resolution_failed",
            )

        return _validate_resolved_addresses(
            url,
            hostname,
            address_infos,
            can_use_private_hosts=can_use_private_hosts,
        )
    except Exception as exc:
        return _build_url_validation_failure(
            url,
            f"URL validation failed: {exc}",
            "validation_failed",
        )


def _can_use_private_hosts() -> bool:
    from shared.core.config import app_config

    return app_config.ENVIRONMENT.lower() in DEVELOPMENT_ENVIRONMENTS


def _extract_resolved_addresses(address_infos: list[AddressInfo]) -> list[str]:
    resolved_addresses: list[str] = []
    seen_addresses: set[str] = set()
    for family, _, _, _, socket_address in address_infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        if not socket_address:
            continue
        address = socket_address[0]
        if not isinstance(address, str):
            continue
        if address and address not in seen_addresses:
            resolved_addresses.append(address)
            seen_addresses.add(address)
    return resolved_addresses


def _is_blocked_hostname(hostname: str) -> bool:
    normalized_hostname = hostname.rstrip(".").lower()
    return normalized_hostname in BLOCKED_HOSTNAMES or normalized_hostname.endswith(
        (".localhost", ".local")
    )


def _is_public_ip_address(ip_address: AllowedIPAddress) -> bool:
    return ip_address.is_global and not _is_blocked_ip_address(ip_address)


def _is_blocked_ip_address(ip_address: AllowedIPAddress) -> bool:
    return (
        ip_address.is_private
        or ip_address.is_loopback
        or ip_address.is_link_local
        or ip_address.is_multicast
        or ip_address.is_reserved
        or ip_address.is_unspecified
    )


def _validate_url_hostname(url: str) -> HTTPURLValidationResult:
    parsed_url = urlparse(url)
    if parsed_url.scheme not in HTTP_SCHEMES:
        return _build_url_validation_failure(
            url,
            f"Unsupported URL scheme: {parsed_url.scheme}",
            "unsupported_scheme",
        )

    hostname = parsed_url.hostname
    if not hostname:
        return _build_url_validation_failure(
            url,
            "URL must include a hostname",
            "missing_hostname",
        )

    return HTTPURLValidationResult(is_valid=True, url=url, hostname=hostname)


def _validate_resolved_addresses(
    url: str,
    hostname: str,
    address_infos: list[AddressInfo],
    *,
    can_use_private_hosts: bool,
) -> HTTPURLValidationResult:
    resolved_addresses = _extract_resolved_addresses(address_infos)
    if not resolved_addresses:
        return _build_url_validation_failure(
            url,
            f"Unable to resolve hostname {hostname}",
            "hostname_resolution_failed",
        )

    for address in resolved_addresses:
        try:
            ip_address = ipaddress.ip_address(address)
        except ValueError:
            return _build_url_validation_failure(
                url,
                f"Hostname {hostname} resolved to invalid IP {address}",
                "invalid_resolved_address",
            )

        if not can_use_private_hosts and not _is_public_ip_address(ip_address):
            return _build_url_validation_failure(
                url,
                f"Hostname {hostname} failed validation",
                "hostname_not_allowed",
            )

    return HTTPURLValidationResult(
        is_valid=True,
        url=url,
        hostname=hostname,
        validated_ip=resolved_addresses[0],
    )


def _build_url_validation_failure(
    url: str,
    error_message: str,
    failure_reason: URLValidationFailureReason,
) -> HTTPURLValidationResult:
    return HTTPURLValidationResult(
        is_valid=False,
        url=url,
        error_message=error_message,
        failure_reason=failure_reason,
    )
