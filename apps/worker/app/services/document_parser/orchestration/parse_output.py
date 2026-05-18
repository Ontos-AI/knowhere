from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class ParseOutput:
    """Parser adapter output with a compatibility view for legacy callers."""

    output_dir: str
    parsed_df: pd.DataFrame | None

    @classmethod
    def from_legacy_tuple(
        cls,
        output_dir: str,
        parsed_df: pd.DataFrame | None,
    ) -> ParseOutput:
        return cls(output_dir=output_dir, parsed_df=parsed_df)

    @property
    def rows_count(self) -> int:
        if self.parsed_df is None:
            return 0
        return len(self.parsed_df)

    def with_dataframe(self, parsed_df: pd.DataFrame | None) -> ParseOutput:
        return ParseOutput(output_dir=self.output_dir, parsed_df=parsed_df)

    def as_legacy_tuple(self) -> tuple[str, pd.DataFrame | None]:
        return self.output_dir, self.parsed_df
