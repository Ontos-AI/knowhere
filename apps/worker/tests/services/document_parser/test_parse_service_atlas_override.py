from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services.document_parser import (
    image_compressor,
    md_parser,
    parse_service,
    pdf_parser,
    txt_parser,
)
from app.services.document_parser.doc_profiler import DocProfile
from shared.core.exceptions.domain_exceptions import ValidationException


def _build_parsed_dataframe() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "content": "hello",
                "path": "doc/test",
                "type": "text",
                "length": 5,
                "keywords": "",
                "summary": "",
                "know_id": "kid",
                "tokens": "",
                "connectto": "",
                "addtime": "now",
                "page_nums": "1",
            }
        ]
    )


def _disable_image_compression(monkeypatch: pytest.MonkeyPatch) -> None:
    compression_stats = SimpleNamespace(
        processed=0,
        converted_png_to_jpg=0,
        resized=0,
        bytes_before=0,
        bytes_after=0,
        rename_map={},
    )
    monkeypatch.setattr(
        image_compressor,
        "compress_output_images",
        lambda output_dir: compression_stats,
    )
    monkeypatch.setattr(
        image_compressor,
        "apply_rename_map_to_dataframe",
        lambda parsed_df, rename_map: parsed_df,
    )


def test_forced_atlas_pdf_bypasses_profile_and_vlm_classifier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "atlas-manual.pdf"
    pdf_path.write_bytes(b"not read because page_count is provided")
    captured_call: dict[str, object] = {}

    def fail_profile(file_full_path: str, internal_output_filename: str) -> DocProfile:
        raise AssertionError("forced atlas PDFs must not use DocProfile")

    def fail_classifier(file_full_path: str) -> bool:
        raise AssertionError("forced atlas PDFs must not use VLM atlas classification")

    def fake_parse_pdfs(
        pdf_path_arg: str,
        filename: str,
        output_dir: str,
        base_llm_paras: dict[str, object],
        profile: DocProfile,
        relative_root: str,
        s3_key: str | None,
    ) -> pd.DataFrame:
        captured_call.update(
            {
                "pdf_path": pdf_path_arg,
                "filename": filename,
                "output_dir": output_dir,
                "profile": profile,
                "relative_root": relative_root,
                "s3_key": s3_key,
            }
        )
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        return _build_parsed_dataframe()

    monkeypatch.setattr(parse_service, "profile_document", fail_profile)
    monkeypatch.setattr(parse_service, "classify_atlas_with_vlm", fail_classifier)
    monkeypatch.setattr(pdf_parser, "parse_pdfs", fake_parse_pdfs)
    _disable_image_compression(monkeypatch)

    full_output_dir, parsed_df = parse_service.checkerboard_inject_parse(
        file_full_path=str(pdf_path),
        filename="atlas-manual.pdf",
        output_dir=str(tmp_path / "output"),
        internal_output_filename="atlas-manual.pdf",
        is_atlas=True,
        page_count=2,
    )

    profile = captured_call["profile"]
    assert isinstance(profile, DocProfile)
    assert profile.file_type == "pdf"
    assert profile.doc_category == "atlas"
    assert profile.page_count == 2
    assert profile.reasoning == "forced_atlas=True"
    assert captured_call["filename"] == "atlas-manual.atlas"
    assert captured_call["relative_root"] == "Default_Root/atlas-manual.atlas"
    assert str(captured_call["output_dir"]).endswith("Default_Root/atlas-manual.atlas")
    assert captured_call["s3_key"] is None
    assert full_output_dir.endswith("Default_Root/atlas-manual.atlas")
    assert not parsed_df.empty


def test_forced_atlas_pdf_still_enforces_page_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "oversized-atlas.pdf"
    pdf_path.write_bytes(b"not read because page_count is provided")

    def fail_parse_pdfs(*args: object, **kwargs: object) -> None:
        raise AssertionError("oversized forced atlas PDFs must fail before parsing")

    monkeypatch.setattr(pdf_parser, "parse_pdfs", fail_parse_pdfs)

    with pytest.raises(ValidationException) as exc_info:
        parse_service.checkerboard_inject_parse(
            file_full_path=str(pdf_path),
            filename="oversized-atlas.pdf",
            output_dir=str(tmp_path / "output"),
            internal_output_filename="oversized-atlas.pdf",
            is_atlas=True,
            page_count=601,
        )

    assert "exceeds the 600-page limit" in exc_info.value.user_message


def test_atlas_flag_is_ignored_for_non_pdf_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    text_path = tmp_path / "notes.txt"
    text_path.write_text("hello", encoding="utf-8")
    profile_calls: list[dict[str, str]] = []
    captured_call: dict[str, object] = {}

    def fake_profile(file_full_path: str, internal_output_filename: str) -> DocProfile:
        profile_calls.append(
            {
                "file_full_path": file_full_path,
                "internal_output_filename": internal_output_filename,
            }
        )
        return DocProfile(
            file_type="txt",
            route="standard",
            decision_band="safe_standard",
            reasoning="txt profile",
        )

    def fail_classifier(file_full_path: str) -> bool:
        raise AssertionError("non-PDF inputs must not use atlas VLM classification")

    def fake_parse_texts(file_path: str, baseurl: str) -> list[str]:
        captured_call["parse_texts_file_path"] = file_path
        return ["hello"]

    def fake_parse_md(
        output_dir: str,
        source_type: str,
        md_lines: list[str] | None = None,
        base_llm_paras: dict[str, object] | None = None,
        relative_root: str | None = None,
        **kwargs: object,
    ) -> pd.DataFrame:
        captured_call.update(
            {
                "output_dir": output_dir,
                "source_type": source_type,
                "md_lines": md_lines,
                "relative_root": relative_root,
            }
        )
        return _build_parsed_dataframe()

    monkeypatch.setattr(parse_service, "profile_document", fake_profile)
    monkeypatch.setattr(parse_service, "classify_atlas_with_vlm", fail_classifier)
    monkeypatch.setattr(txt_parser, "parse_texts", fake_parse_texts)
    monkeypatch.setattr(md_parser, "parse_md", fake_parse_md)
    _disable_image_compression(monkeypatch)

    full_output_dir, parsed_df = parse_service.checkerboard_inject_parse(
        file_full_path=str(text_path),
        filename="notes.txt",
        output_dir=str(tmp_path / "output"),
        internal_output_filename="notes.txt",
        is_atlas=True,
        page_count=1,
    )

    assert profile_calls == [
        {
            "file_full_path": str(text_path),
            "internal_output_filename": "notes.txt",
        }
    ]
    assert captured_call["parse_texts_file_path"] == str(text_path)
    assert captured_call["relative_root"] == "Default_Root/notes.txt"
    assert str(captured_call["output_dir"]).endswith("Default_Root/notes.txt")
    assert full_output_dir.endswith("Default_Root/notes.txt")
    assert not full_output_dir.endswith(".atlas")
    assert not parsed_df.empty
