"""Security-related shared utilities."""

from typing import Optional


def mask_api_key(api_key: Optional[str]) -> str:
    """Return a stable masked representation for logs and internal errors."""
    if not api_key:
        return "<unset>"

    if len(api_key) <= 4:
        return "*" * len(api_key)

    if len(api_key) <= 8:
        prefix_length = 2
        suffix_length = 2
    else:
        prefix_length = 4
        suffix_length = 4

    masked_length = max(1, len(api_key) - prefix_length - suffix_length)
    return f"{api_key[:prefix_length]}{'*' * masked_length}{api_key[-suffix_length:]}"
