"""Immutable object-storage references for one canonical demo version."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CanonicalDemoBundle:
    zip_key: str
    zip_size: int
    raw_prefix: str
    content_version: str
    reused: bool
