"""Calibration package: deterministic Phase-1 scan + production Phase-2.

Keep this module import-light: ``structure.section_page_verify`` imports
``calibration.prompts``, and heavy eager imports here recreate a circular
import through ``tools`` → ``anchoring_primitives``.
"""

from typing import Any

__all__ = [
    "CalibrationResult",
    "build_calibration_payload",
    "calibrate_offset",
    "finalize_calibration_result",
    "run_calibration_phase1",
    "scan_title_forward",
]


def __getattr__(name: str) -> Any:
    if name == "CalibrationResult":
        from app.services.document_agent.calibration.types import (
            CalibrationResult,
        )

        return CalibrationResult
    if name == "build_calibration_payload":
        from app.services.document_agent.calibration.procedure import (
            build_calibration_payload,
        )

        return build_calibration_payload
    if name == "calibrate_offset":
        from app.services.document_agent.calibration.service import (
            calibrate_offset,
        )

        return calibrate_offset
    if name == "finalize_calibration_result":
        from app.services.document_agent.calibration.procedure import (
            finalize_calibration_result,
        )

        return finalize_calibration_result
    if name == "run_calibration_phase1":
        from app.services.document_agent.calibration.phase1 import (
            run_calibration_phase1,
        )

        return run_calibration_phase1
    if name == "scan_title_forward":
        from app.services.document_agent.calibration.scan import (
            scan_title_forward,
        )

        return scan_title_forward
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
