from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _contract_environment(worker_contract_environment: None) -> None:
    del worker_contract_environment


@pytest.mark.parametrize("ingestion_mode", ["progressive", "full"])
def test_media_parse_execution_uses_checkerboard_for_full_and_progressive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ingestion_mode: str,
) -> None:
    from app.services.document_ingestion import parse_execution
    from app.services.document_ingestion.processing_context import ParseJobContext
    from app.services.document_ingestion.source_preparation import PreparedSourceFile
    from app.services.document_parser.orchestration.parse_output import ParseOutput

    calls: list[str] = []
    monkeypatch.setattr(
        parse_execution,
        "_execute_chunk_baseline_parse",
        lambda **_: calls.append("chunk_baseline")
        or ParseOutput(output_dir="", parsed_df=None),
    )
    monkeypatch.setattr(
        parse_execution,
        "_execute_sheet_baseline_parse",
        lambda **_: calls.append("sheet_baseline")
        or ParseOutput(output_dir="", parsed_df=None),
    )
    monkeypatch.setattr(
        parse_execution.parse_service,
        "checkerboard_parse_output",
        lambda **_: calls.append("checkerboard")
        or ParseOutput(output_dir="", parsed_df=None),
    )
    source = tmp_path / "talk.mp3"
    source.write_bytes(b"id3")
    parse_execution.execute_document_parse(
        job_id="job",
        job_context=ParseJobContext(
            job_metadata={"parse_track": "media", "ingestion_mode": ingestion_mode},
            job_user_id="u1",
            metadata_service=None,  # pyright: ignore[reportArgumentType]
            redis_service=None,
            s3_key="uploads/talk.mp3",
        ),
        prepared_source=PreparedSourceFile(
            source_file_name="talk.mp3",
            internal_parse_name="talk.mp3",
            local_file_path=str(source),
            file_extension=".mp3",
        ),
        output_dir=str(tmp_path / "out"),
    )

    assert calls == ["checkerboard"]
