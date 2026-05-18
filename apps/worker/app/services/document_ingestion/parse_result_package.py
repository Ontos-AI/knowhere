from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
from loguru import logger

from shared.core.exceptions.domain_exceptions import WorkerHandlingException
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks


@dataclass(frozen=True)
class ParseArtifact:
    add_dir: str
    dataframe: pd.DataFrame

    @property
    def contents_count(self) -> int:
        return len(self.dataframe)


@dataclass(frozen=True)
class ParseResultPackage:
    artifact: ParseArtifact
    chunks: list[dict[str, Any]]


@dataclass(frozen=True)
class GeneratedResultPackage:
    zip_file_path: str
    checksum_value: str
    statistics: dict[str, Any]
    zip_size: int

    @classmethod
    def from_legacy_tuple(
        cls,
        zip_file_path: str,
        checksum: dict[str, Any] | str | None,
        statistics: dict[str, Any],
        zip_size: int,
    ) -> GeneratedResultPackage:
        checksum_value = (
            str(checksum.get("value", ""))
            if isinstance(checksum, dict)
            else str(checksum or "")
        )
        return cls(
            zip_file_path=zip_file_path,
            checksum_value=checksum_value,
            statistics=statistics,
            zip_size=zip_size,
        )


def build_parse_result_package(
    *,
    job_id: str,
    filename: str,
    add_dir: str,
    parsed_contents_df: pd.DataFrame | None,
) -> ParseResultPackage:
    artifact = _build_parse_artifact(
        job_id=job_id,
        filename=filename,
        add_dir=add_dir,
        parsed_contents_df=parsed_contents_df,
    )
    chunks = dataframe_to_chunks(artifact.dataframe)
    return ParseResultPackage(artifact=artifact, chunks=chunks)


def _build_parse_artifact(
    *,
    job_id: str,
    filename: str,
    add_dir: str,
    parsed_contents_df: pd.DataFrame | None,
) -> ParseArtifact:
    if parsed_contents_df is None:
        raise WorkerHandlingException(
            user_message="We could not extract content from your file",
            internal_message="File parsing failed, no content returned from parser",
        )

    if parsed_contents_df.empty:
        logger.warning(
            f"No content returned from file parsing: job_id={job_id}, filename={filename}"
        )

    return ParseArtifact(add_dir=add_dir, dataframe=parsed_contents_df)
