"""Garbage-collection utilities shared across services."""

import gc
import importlib
from types import ModuleType

_torch: ModuleType | bool | None = None


def _get_torch() -> ModuleType | None:
    global _torch
    if _torch is None:
        try:
            _torch = importlib.import_module("torch")
        except ImportError:
            _torch = False
    return _torch if isinstance(_torch, ModuleType) else None


def gc_collect():
    """
    Run garbage collection, including CUDA cache cleanup when torch is available.
    """
    gc.collect()
    torch = _get_torch()
    if torch and torch.cuda.is_available():
        torch.cuda.empty_cache()
