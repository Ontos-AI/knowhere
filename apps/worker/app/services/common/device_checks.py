"""Shared device-check helpers used by multiple services."""

import importlib
from typing import Any, Dict

import requests
from loguru import logger


def check_internet(url: str = "http://www.baidu.com") -> bool:
    """
    Check internet connectivity.

    Args:
        url: URL to probe.

    Returns:
        Whether the probe succeeded.
    """
    try:
        from shared.core.constants import APIConstants

        response = requests.get(url, timeout=APIConstants.REQUEST_TIMEOUT)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.warning(f"Network connectivity check failed: {e}")
        return False


def check_device_capabilities() -> Dict[str, Any]:
    """
    Check local device capabilities.

    Returns:
        Device info including:
        - device: Device type ("cuda" or "cpu")
        - has_internet: Whether internet access is available
        - can_use_local_llm: Whether a local LLM is practical
        - can_use_local_summary: Whether local summarization is practical

    torch is imported lazily to avoid forcing it into API startup paths.
    """
    # Import torch lazily to avoid forcing it into API service startup.
    try:
        torch = importlib.import_module("torch")

        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        # Default to CPU when torch is unavailable, such as in the API service.
        logger.debug("torch is unavailable; defaulting to the CPU device")
        device = "cpu"
    except Exception as e:
        # Handle other failures, such as torch initialization errors.
        logger.warning(
            f"Error while checking device capabilities: {e}; defaulting to the CPU device"
        )
        device = "cpu"

    device_info = {
        "device": device,
        "has_internet": check_internet(),
        "can_use_local_llm": False,
        "can_use_local_summary": False,
    }

    if device_info["device"] == "cuda":
        device_info["can_use_local_llm"] = True
        device_info["can_use_local_summary"] = True
    elif device_info["device"] == "cpu":
        device_info["can_use_local_llm"] = False
        device_info["can_use_local_summary"] = False

    return device_info
