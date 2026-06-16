import json
import zipfile
from pathlib import Path

from PIL import Image

from app.services.document_parser import atlas_parser
from shared.core.config import settings
from shared.services.redis.redis_sync_service import SyncChunksRedisService
from shared.services.storage.zip_result_service import ZipResultService


def test_parse_atlas_packages_unique_images_for_duplicate_page_titles(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "parsed"

    def fake_run_in_child_process(worker_func, *args, **kwargs):
        if worker_func is atlas_parser._atlas_extract_texts_worker:
            return {
                "total_pages": 2,
                "page_texts": ["page one text", "page two text"],
            }

        if worker_func is atlas_parser._atlas_render_pages_worker:
            img_dir = Path(args[1])
            img_dir.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (16, 16), color=(10, 20, 30)).save(img_dir / "page-1.jpg")
            Image.new("RGB", (16, 16), color=(40, 50, 60)).save(img_dir / "page-2.jpg")
            return {
                "page_data": [
                    (1, "page one text", "page-1.jpg"),
                    (2, "page two text", "page-2.jpg"),
                ]
            }

        raise AssertionError(f"Unexpected worker: {worker_func}")

    monkeypatch.setattr(atlas_parser, "run_in_child_process", fake_run_in_child_process)
    monkeypatch.setattr(
        atlas_parser,
        "_detect_toc_pages_from_texts",
        lambda page_texts, model_name: (set(), None),
    )
    monkeypatch.setattr(
        atlas_parser,
        "_vlm_extract_page_info",
        lambda output_dir, img_name: "Mock atlas page info",
    )

    parsed_df = atlas_parser.parse_atlas(
        pdf_path=str(tmp_path / "synthetic.pdf"),
        output_dir=str(output_dir),
        base_llm_paras={
            "model_name": "test-model",
            "stopwords": [],
        },
        relative_root="Default_Root/synthetic.atlas",
    )

    split_char = settings.SPLIT_CHAR or "/"
    assert parsed_df["path"].tolist() == [
        f"Default_Root/synthetic.atlas{split_char}Mock atlas page info",
        f"Default_Root/synthetic.atlas{split_char}Mock atlas page info_2",
    ]

    packaged_images = sorted(path.name for path in (output_dir / "images").iterdir())
    assert packaged_images == [
        "Mock atlas page info.jpg",
        "Mock atlas page info_2.jpg",
    ]

    chunks = SyncChunksRedisService(None).dataframe_to_chunks(parsed_df)

    zip_file_path, _, _, _ = ZipResultService().generate_zip_package(
        job_id="atlas-duplicate-title-repro",
        chunks=chunks,
        add_dir=str(output_dir),
        source_file_name="synthetic.pdf",
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

    assert zip_image_entries == [
        "images/Mock atlas page info.jpg",
        "images/Mock atlas page info_2.jpg",
    ]
    assert [chunk["metadata"]["file_path"] for chunk in chunk_payload] == zip_image_entries
    assert [chunk["content"] for chunk in chunk_payload] == [
        f"IMAGE_{chunk_payload[0]['chunk_id']}_IMAGE\nMock atlas page info\npage one text",
        f"IMAGE_{chunk_payload[1]['chunk_id']}_IMAGE\nMock atlas page info\npage two text",
    ]
