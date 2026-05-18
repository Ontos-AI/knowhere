from __future__ import annotations

import uuid
from datetime import datetime


def gen_str_codes(input_string: str) -> str:
    """Generate a UUID5 code from a string."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, input_string))


def get_str_time() -> str:
    """Get the current time as a string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
