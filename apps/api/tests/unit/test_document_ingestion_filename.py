from app.services.document_ingestion.creation_service import (
    _resolve_url_source_file_name,
)


def test_resolve_url_source_file_name_decodes_percent_encoded_path() -> None:
    source_url = (
        "https://files.example.test/"
        "%E4%B8%AD%E6%96%87%E7%AE%80%E5%8E%86-%E9%9B%B7%E7%BF%94-"
        "%E4%B8%AD%E7%A7%91%E9%99%A2%285%29.doc"
    )

    source_file_name = _resolve_url_source_file_name(
        source_url=source_url,
        file_extension=".doc",
    )

    assert source_file_name == "中文简历-雷翔-中科院(5).doc"
