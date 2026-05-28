from __future__ import annotations

from app.services.document_parser.support.parser_rows import ParsedRow


def build_image_asset_row(
    *,
    content: str,
    relative_path: str,
    summary: str,
    know_id: str,
    addtime: str,
) -> ParsedRow:
    return ParsedRow(
        content=content,
        path=relative_path,
        type="image",
        keywords="",
        summary=summary,
        know_id=know_id,
        tokens="",
        connectto="",
        addtime=addtime,
    )


def build_table_asset_row(
    *,
    content: str,
    relative_path: str,
    summary: str,
    keywords: str,
    know_id: str,
    addtime: str,
) -> ParsedRow:
    return ParsedRow(
        content=content,
        path=relative_path,
        type="table",
        keywords=keywords,
        summary=summary,
        know_id=know_id,
        tokens="",
        connectto="",
        addtime=addtime,
    )

