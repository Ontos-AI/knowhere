"""Turn terminal Cursor run failures into a safe retrieval API error."""

from __future__ import annotations

import re
from typing import Literal

from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.core.response.ErrorCode import ErrorCode

_DIAGNOSTIC_TOKEN: re.Pattern[str] = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}")


class CursorRetrievalException(KnowhereException):
    def __init__(
        self,
        *,
        model: str,
        status: Literal["error", "cancelled"],
        provider_message: str | None,
        provider_code: str | None,
        run_id: str | None,
        request_id: str | None,
    ) -> None:
        reason: str
        user_message: str
        if status == "cancelled":
            reason = "cancelled"
            user_message = "Deep search failed because Cursor cancelled the retrieval run."
        elif provider_message and "out of usage" in provider_message.casefold():
            reason = "usage_limit"
            user_message = "Deep search is unavailable because Cursor's model usage limit was reached."
        else:
            reason = "provider_error"
            user_message = "Deep search failed because Cursor could not complete the retrieval run."

        # Provider messages are untrusted. Keep only the category and bounded identifiers.
        diagnostic_fields: dict[str, str | None] = {
            "cursor_model": model,
            "cursor_status": status,
            "cursor_error_code": provider_code,
            "cursor_run_id": run_id,
            "cursor_request_id": request_id,
        }
        self._diagnostic_fields: dict[str, str] = {
            name: value
            for name, value in diagnostic_fields.items()
            if isinstance(value, str)
            and _DIAGNOSTIC_TOKEN.fullmatch(value)
            and "crsr_" not in value.casefold()
        }
        diagnostics: str = " ".join(
            f"{name}={value}" for name, value in self._diagnostic_fields.items()
        )
        super().__init__(
            code=ErrorCode.UNAVAILABLE,
            user_message=user_message,
            internal_message=f"Cursor Deep search failed reason={reason} {diagnostics}",
            details={"service": "cursor", "reason": reason},
        )

    def to_log(self) -> dict[str, object]:
        return {**super().to_log(), **self._diagnostic_fields}
