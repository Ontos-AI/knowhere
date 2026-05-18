"""Small logging helpers for document parser modules."""


def truncate_log_value(value: object, max_length: int = 2000) -> str | None:
    """Trim large values before storing them in structured logs."""
    if value is None:
        return None

    text = str(value).strip()
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}...<truncated>"
