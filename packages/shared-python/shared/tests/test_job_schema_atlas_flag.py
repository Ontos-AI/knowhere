import os

os.environ.setdefault("DS_KEY", "test-key")
os.environ.setdefault("DS_URL", "https://example.com")
os.environ.setdefault("S3_BUCKET_NAME", "test-bucket")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test-access-key")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test-secret-key")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")
os.environ.setdefault("USERS_DATA_PATH", "/tmp")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/testdb")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("TMP_PATH", "/tmp")
os.environ.setdefault("FONT_PATH", "/tmp/font.ttf")
os.environ.setdefault("CHROMEDRIVER_PATH", "/tmp/chromedriver")

from shared.models.schemas.job import JobCreate, ParsingParams
from shared.models.schemas.job_metadata import JobMetadataHelper


def test_parsing_params_preserves_explicit_atlas_flag() -> None:
    parsing_params = ParsingParams(is_atlas=True)
    request = JobCreate(
        source_type="file",
        file_name="atlas.pdf",
        parsing_params=parsing_params,
    )

    metadata = JobMetadataHelper.create_from_request(request)

    assert parsing_params.model_dump()["is_atlas"] is True
    assert metadata["parsing_params"]["is_atlas"] is True
    assert JobMetadataHelper.get_parsing_param(metadata, "is_atlas", False) is True
