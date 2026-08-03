from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from shared.services.chunks.canonical_chunk_builder import ChunkPayload


@dataclass(frozen=True)
class ParseOutput:
    """Parser adapter output.

    Page Memory prefers ``chunks``. Text Track continues to emit ``parsed_df``,
    which is adapted to chunks downstream.
    """

    output_dir: str
    parsed_df: pd.DataFrame | None = None
    chunks: list[ChunkPayload] | None = None

    @property
    def rows_count(self) -> int:
        if self.chunks is not None:
            return len(self.chunks)
        if self.parsed_df is None:
            return 0
        return len(self.parsed_df)

    def with_dataframe(self, parsed_df: pd.DataFrame | None) -> ParseOutput:
        return ParseOutput(
            output_dir=self.output_dir,
            parsed_df=parsed_df,
            chunks=self.chunks,
        )

    def with_chunks(self, chunks: list[ChunkPayload] | None) -> ParseOutput:
        return ParseOutput(
            output_dir=self.output_dir,
            parsed_df=self.parsed_df,
            chunks=chunks,
        )
