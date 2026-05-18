import json
from typing import Any, Dict, Optional

from loguru import logger


def normalize_error_details(raw_details: Any) -> Optional[Dict[str, Any]]:
    """Normalize error details into a dictionary when possible."""
    if raw_details is None:
        return None

    if isinstance(raw_details, dict):
        return raw_details

    if isinstance(raw_details, str):
        try:
            parsed_details: Any = json.loads(raw_details)
        except json.JSONDecodeError:
            logger.warning("Ignoring non-JSON error details string")
            return None

        if isinstance(parsed_details, dict):
            return parsed_details

        logger.warning(
            "Ignoring JSON error details with unsupported root type: {}",
            type(parsed_details).__name__,
        )
        return None

    logger.warning(
        "Ignoring error details with unsupported type: {}",
        type(raw_details).__name__,
    )
    return None
