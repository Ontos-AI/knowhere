"""Optional artifact location arguments for legacy and canonical result readers."""

from typing import TypedDict


class RawPrefixArguments(TypedDict, total=False):
    raw_prefix: str
