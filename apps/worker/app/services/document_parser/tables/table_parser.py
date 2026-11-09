from __future__ import annotations

import pandas as pd


def parse_xlsx(
    file_path: str,
    file_name: str,
    output_dir: str,
    baseurl: str,
    base_llm_paras: dict[str, object] | None = None,
    relative_root: str | None = None,
    include_hidden_sheets: bool = False,
) -> pd.DataFrame:
    from app.services.document_parser.formats.excel.table_parser import (
        parse_xlsx as parse_excel_xlsx,
    )

    return parse_excel_xlsx(
        file_path=file_path,
        file_name=file_name,
        output_dir=output_dir,
        baseurl=baseurl,
        base_llm_paras=base_llm_paras,
        relative_root=relative_root,
        include_hidden_sheets=include_hidden_sheets,
    )
