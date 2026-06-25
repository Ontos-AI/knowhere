from __future__ import annotations

from app.services.document_parser.support.parser_rows import ParsedRow


def build_image_asset_row(
    *,
    content: str,
    relative_path: str,
    summary: str,
    know_id: str,
    addtime: str,
    keywords: str = "",
    entities: str = "",
    asset_title: str = "",
) -> ParsedRow:
    return ParsedRow(
        content=content,
        path=relative_path,
        type="image",
        keywords=keywords,
        summary=summary,
        know_id=know_id,
        tokens="",
        connectto="",
        addtime=addtime,
        entities=entities,
        asset_title=asset_title,
    )


def build_table_asset_row(
    *,
    relative_path: str,
    summary: str,
    keywords: str,
    know_id: str,
    addtime: str,
    entities: str = "",
    asset_title: str = "",
) -> ParsedRow:
    row_content = relative_path
    return ParsedRow(
        content=row_content,
        path=relative_path,
        type="table",
        keywords=keywords,
        summary=summary,
        know_id=know_id,
        tokens="",
        connectto="",
        addtime=addtime,
        length=len(row_content),
        entities=entities,
        asset_title=asset_title,
    )
