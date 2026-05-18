from __future__ import annotations

import pandas as pd
from app.services.document_parser.tables.table_frame_parser import (
    format_tb_scope as format_tb_scope,
    multiindex_to_tree as multiindex_to_tree,
    parse_headers as parse_headers,
    parse_tb_contents as parse_tb_contents,
    parse_tb_keywords as parse_tb_keywords,
    postprocess_tb as postprocess_tb,
    process_datetime_cells as process_datetime_cells,
    process_duplicate_cols as process_duplicate_cols,
    tb_columns_to_tree as tb_columns_to_tree,
)
from app.services.document_parser.tables.table_text_parser import (
    clean_html_tb as clean_html_tb,
    df2md as df2md,
    extract_tables_by_forms as extract_tables_by_forms,
    identify_tables as identify_tables,
    sanitize_table_name_from_header as sanitize_table_name_from_header,
)


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
