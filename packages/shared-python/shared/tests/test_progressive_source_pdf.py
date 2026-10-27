"""Progressive snapshots retain the converted PDF for lazy cited-page rendering."""

from pathlib import Path
from zipfile import ZipFile

from shared.services.storage.zip_result_service import ZipResultService


def test_progressive_package_retains_source_pdf_without_rendering_pages(
    tmp_path: Path,
) -> None:
    source = b"immutable converted PDF bytes"
    (tmp_path / "source.pdf").write_bytes(source)
    path, _, _, _ = ZipResultService().generate_zip_package(
        job_id="j",
        chunks=[],
        add_dir=str(tmp_path),
        source_file_name="slides.pptx",
        data_id=None,
        job_metadata={"ingestion_mode": "progressive", "parse_track": "page_memory"},
        temp_dir=str(tmp_path),
    )
    with ZipFile(path) as archive:
        assert archive.read("source.pdf") == source
        assert not any(name.endswith(".png") for name in archive.namelist())
