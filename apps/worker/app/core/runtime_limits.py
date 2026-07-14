"""Worker runtime limits for parser subprocesses and native thread pools."""

from collections.abc import MutableMapping
import os

NATIVE_THREAD_CAPS = {
    "OMP_NUM_THREADS": "1",
    "OMP_THREAD_LIMIT": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "BLIS_NUM_THREADS": "1",
}


def read_pymupdf_max_concurrent() -> int:
    """Read the per-pod PyMuPDF child-process cap from shared settings."""
    environment_value = os.environ.get("PYMUPDF_MAX_CONCURRENT")
    if environment_value is not None:
        value = int(environment_value)
        if value < 1:
            raise ValueError("PYMUPDF_MAX_CONCURRENT must be positive")
        return value

    from shared.core.config import settings

    return settings.PYMUPDF_MAX_CONCURRENT


def apply_native_thread_caps(
    env: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Force conservative native thread caps to protect the main worker loop."""
    import os

    target = env if env is not None else os.environ
    applied_caps: dict[str, str] = {}

    for env_var, default_value in NATIVE_THREAD_CAPS.items():
        target[env_var] = default_value
        applied_caps[env_var] = default_value

    return applied_caps
