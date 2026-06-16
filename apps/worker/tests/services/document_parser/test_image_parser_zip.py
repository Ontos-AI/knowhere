import json
import zipfile
from pathlib import Path

from PIL import Image

from app.services.document_parser import image_parser
from shared.core.constants import ProcessingConstants
from shared.services.redis.redis_sync_service import SyncChunksRedisService
from shared.services.storage.zip_result_service import ZipResultService


def test_parse_image_packages_standalone_image_under_images_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source_image_path = tmp_path / "source.png"
    Image.new("RGB", (32, 32), color=(12, 34, 56)).save(source_image_path)

    monkeypatch.setattr(ProcessingConstants, "IMG_MIN_SIZE", 1)
    monkeypatch.setattr(image_parser, "_get_vision_client", lambda: object())

    def fake_ask_image(
        client,
        kb_dir,
        paths_,
        title_text="",
        task="summary-images",
        query="",
        max_tokens=None,
        size_cut=True,
    ):
        assert paths_ == ["images/source.png"]
        assert task == "judge-image-type"
        return {"answer": "photo"}

    monkeypatch.setattr(image_parser, "ask_image", fake_ask_image)

    output_dir = tmp_path / "parsed"
    parsed_df = image_parser.parse_image(
        image_path=str(source_image_path),
        filename="source.png",
        output_dir=str(output_dir),
        base_llm_paras={
            "frag_desc": "",
            "summary_image": False,
        },
        relative_root="Default_Root/source.png",
    )

    packaged_images = sorted(path.name for path in (output_dir / "images").iterdir())
    assert packaged_images == ["source.png"]

    chunks = SyncChunksRedisService(None).dataframe_to_chunks(parsed_df)

    zip_file_path, _, _, _ = ZipResultService().generate_zip_package(
        job_id="standalone-image-repro",
        chunks=chunks,
        add_dir=str(output_dir),
        source_file_name="source.png",
        data_id=None,
        job_metadata={},
        parsed_df=parsed_df,
        temp_dir=str(tmp_path),
    )

    with zipfile.ZipFile(zip_file_path) as zip_file:
        zip_image_entries = sorted(
            name for name in zip_file.namelist() if name.startswith("images/")
        )
        chunk_payload = json.loads(zip_file.read("chunks.json"))["chunks"]

    assert zip_image_entries == [f"images/{packaged_images[0]}"]
    assert chunk_payload[0]["content"].splitlines()[0] == (
        f"IMAGE_{chunk_payload[0]['chunk_id']}_IMAGE"
    )
    assert chunk_payload[0]["metadata"]["file_path"] == zip_image_entries[0]
