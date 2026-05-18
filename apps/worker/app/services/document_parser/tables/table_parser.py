from __future__ import annotations

import pandas as pd


def parse_xlsx(
    file_path: str,
    file_name: str,
    output_dir: str,
    baseurl: str,
    base_llm_paras: dict[str, object] | None = None,
    window_h: int = 10,
    relative_root: str | None = None,
    use_precision_mode: bool = True,
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
        window_h=window_h,
        relative_root=relative_root,
        use_precision_mode=use_precision_mode,
        include_hidden_sheets=include_hidden_sheets,
    )
