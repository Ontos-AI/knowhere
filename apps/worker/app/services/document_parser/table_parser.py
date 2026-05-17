# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportOptionalOperand=false, reportOptionalSubscript=false, reportReturnType=false
import datetime
import io
import os
import re
import threading
import uuid
from collections import OrderedDict

import numpy as np
import pandas as pd
from app.services.document_parser.dataframe_helpers import process_dup_paths_df
from app.services.document_parser.excel_structure_parser import parse_excel_structure
from app.services.document_parser.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.parser_rows import ParsedRow, ParsedRowsBuilder
from app.services.document_parser.path_helpers import flatten_dic2paths, remove_spaces
from app.services.document_parser.table_asset_writer import (
    TableAssetInput,
    write_table_asset,
)
from app.services.document_parser.dataframe_html_renderer import df2html
from bs4 import BeautifulSoup
from loguru import logger

from shared.core.exceptions.domain_exceptions import TableParsingException
from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.services.ai.prompt_service import build_prompt
from shared.services.ai.response_process_service import eval_response
from shared.utils.chunk_refs import build_chunk_ref
from shared.utils.file_loading import load_file_bytes
from shared.utils.file_utils import path_handle
from shared.utils.OpenAICompatibleClientSync import get_openai_client
from shared.utils.text_utils import remove_duplicates_orderkept, tokenize2stw_remove

# ── Table filename sanitizer ────────────────────────────
# Max byte-safe filename length. Most filesystems cap at 255 bytes; we leave
# room for the "table-N " prefix (~10 chars) and ".html" suffix (5 chars).
_MAX_TABLE_NAME_CHARS = 80


def sanitize_table_name_from_header(raw_header_text: str) -> str:
    """Build a concise, filesystem-safe table name from raw first-row header text.

    Pipeline:
      1. Split by common delimiters (' | ', '_br_'/'__br_', '\\n')
      2. Strip whitespace, deduplicate (preserve order)
      3. Drop trivial single-character tokens (single CJK char, single digit,
         single letter) — reuses ``_is_meaningful_token`` from shared text_utils
      4. Rejoin with spaces and cap at ``_MAX_TABLE_NAME_CHARS``

    Args:
        raw_header_text: The raw first-row text, often pipe-separated.

    Returns:
        A cleaned string suitable for use in a filename (may be empty if all
        fields were trivial).
    """
    from shared.utils.text_utils import _is_meaningful_token

    if not raw_header_text:
        return ""

    # 1. Split on common header delimiters
    parts = re.split(r"\s*\|\s*|_+br_|\n", raw_header_text)

    # 2. Strip + deduplicate (order-preserved)
    seen: set[str] = set()
    unique: list[str] = []
    for p in parts:
        p = p.strip()
        if not p or p in seen:
            continue
        seen.add(p)
        unique.append(p)

    # 3. Keep only meaningful fields (drop single-char noise)
    meaningful = [f for f in unique if _is_meaningful_token(f)]

    # 4. Join and enforce length cap
    result = " ".join(meaningful)
    if len(result) > _MAX_TABLE_NAME_CHARS:
        result = result[:_MAX_TABLE_NAME_CHARS].rstrip()
    return result


g_tbl_lock = threading.Lock()


def identify_tables(line):
    """Identify if a line contains a table.

    Note: For HTML tables, use merge_html_tables() from html_parser.py
    to preprocess multi-line tables before calling this function.
    """
    # HTML table: complete <table>...</table> in one line
    html_tb_pattern = r"<table.*?>.*?</table>"
    tables = re.findall(html_tb_pattern, line, re.DOTALL)
    if bool(tables):
        return True, "html", tables

    # MD table: lines starting and ending with |
    if line.startswith("|") and line.endswith("|"):
        return True, "md", []

    return False, None, None


def df2md(tb_df: pd.DataFrame, *, index: bool = False, na_rep: str = "—") -> str:
    """Convert DataFrame to Markdown table format with dynamic column widths.

    Note: Truncation should be done externally using truncate_text before calling this function.

    Args:
        tb_df: Input DataFrame
        index: Whether to include index column
        na_rep: String to represent NA values

    Returns:
        Markdown table string
    """
    import unicodedata

    def get_display_width(text: str) -> int:
        """eval width for both ASCII and Chinese"""
        width = 0
        for char in text:
            if unicodedata.east_asian_width(char) in ("F", "W"):
                width += 2
            else:
                width += 1
        return width

    def pad_to_width(text: str, target_width: int) -> str:
        current_width = get_display_width(text)
        padding = target_width - current_width
        return text + " " * max(0, padding)

    df = tb_df.copy()

    # Handle index
    if index:
        df = df.reset_index()

    # Replace NA values
    df = df.fillna(na_rep)

    # Convert all values to string
    df = df.astype(str)

    # Calculate column widths based on actual display width (no truncation)
    col_widths = {}
    for col in df.columns:
        header_width = get_display_width(str(col))
        max_content_width = max(df[col].apply(get_display_width)) if len(df) > 0 else 0
        col_widths[col] = max(header_width, max_content_width)

    # Build header row
    header_cells = [pad_to_width(str(col), col_widths[col]) for col in df.columns]
    header_line = "| " + " | ".join(header_cells) + " |"

    # Build separator row
    separator_cells = ["-" * col_widths[col] for col in df.columns]
    separator_line = "|-" + "-|-".join(separator_cells) + "-|"

    # Build data rows
    data_lines = []
    for _, row in df.iterrows():
        cells = [pad_to_width(str(row[col]), col_widths[col]) for col in df.columns]
        data_lines.append("| " + " | ".join(cells) + " |")

    # Combine all parts
    lines = [header_line, separator_line] + data_lines
    return "\n".join(lines)


def clean_html_tb(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for row in soup.find_all("tr"):
        seen = set()
        unique_cells = []
        for cell in row.find_all("td", recursive=False):
            content = cell.encode_contents()
            if content not in seen:
                seen.add(content)
                unique_cells.append(cell)
        row.clear()
        for cell in unique_cells:
            row.append(cell)
    return soup.prettify()


def extract_tables_by_forms(tb_txt, form):
    if form == "html":
        return tb_txt
    elif form == "md":
        tb_df = pd.read_table(
            pd.io.common.StringIO(tb_txt), sep="|", engine="python", on_bad_lines="skip"
        )
        tb_df = tb_df.drop(columns=tb_df.columns[0])  # Drop extra leading column
        tb_df = tb_df.drop(columns=tb_df.columns[-1])  # Drop extra trailing column
        tb_df.columns = tb_df.columns.str.strip()  # Clean up headers
        # Filter out MD separator lines (e.g. "---", ":---:", "---:")
        separator_pattern = r"^[\s\-:]+$"
        tb_df = tb_df[
            ~tb_df.apply(
                lambda row: row.astype(str).str.match(separator_pattern).all(), axis=1
            )
        ]
        tb_strs = tb_df.to_html(index=False)
    else:
        tb_strs = None  # UNDER DEVELOPMENT other forms of tables...
    return tb_strs


def parse_headers(df_temp, paras=None, header_window=5, smart_headers=True):
    def parse_headers_nonsmart(df_):
        non_na_row = df_[df_.notna().any(axis=1)].head(1)
        header_id = non_na_row.index[-1] if not non_na_row.empty else None
        header_rows = list(range(header_id + 1))
        return header_rows

    if not pd.isna(
        df_temp.columns
    ).all():  # If columns are not all NaN, no need to add extra row
        df_temp.loc[-1] = df_temp.columns
        df_temp.index = df_temp.index + 1
        df_temp = df_temp.sort_index()
        df_temp.columns = [np.nan] * df_temp.shape[1]

    if paras["summary_table"] and smart_headers:
        try:
            tb_small = df_temp.head(header_window)
            tb_small_str = df2html(tb_small)
            prompt, temperature, top_p, max_tokens = build_prompt(
                task="detect-table-headers", texts=tb_small_str, query="", paras=paras
            )

            messages = [
                {"role": "system", "content": "You are a helpful assistant"},
                {"role": "user", "content": prompt},
            ]

            ctx_task_id = gen_str_codes((str(uuid.uuid4()) + tb_small_str))

            # Track task status via Redis (skip in LOCAL_DEBUG mode)
            import os

            if os.getenv("LOCAL_DEBUG", "0") != "1":
                from shared.services.redis.redis_sync_service import (
                    SyncRedisServiceFactory,
                )

                redis_service = SyncRedisServiceFactory.get_service()
                redis_service.set(f"task:{ctx_task_id}:status", "processing", ttl=7200)

            # Use unified AI service
            header_res = get_openai_client().chat_completion(
                messages=messages, timeout=60
            )
            header_res = eval_response(header_res)
            # Extract answer field
            if isinstance(header_res, dict):
                answer = header_res.get("answer", [])
            else:
                answer = header_res if isinstance(header_res, list) else []

            # Check if answer is empty list
            if not answer or len(answer) == 0:
                logger.warning(
                    "AI returned empty list, cannot identify headers, falling back to traditional mode..."
                )
                header_rows = parse_headers_nonsmart(df_temp)
            else:
                try:
                    header_id = answer[-1]
                    header_rows = list(range(header_id + 1))
                except Exception as e:
                    logger.warning(
                        f"Failed to parse header row number: {e}, falling back to traditional mode..."
                    )
                    header_rows = parse_headers_nonsmart(df_temp)

        except Exception as e:
            logger.warning(
                f"Smart header parsing failed: {e}, falling back to traditional mode..."
            )
            header_rows = parse_headers_nonsmart(df_temp)
    else:
        header_rows = parse_headers_nonsmart(df_temp)

    # improve table structure based on header rows
    if len(header_rows) == 0 or (all(h is None for h in header_rows)):
        logger.warning("No valid headers detected, fallback to using row 0 as header")
        new_header = df_temp.iloc[0].ffill().bfill().tolist()
        df_temp.columns = new_header
        df_temp = df_temp.iloc[1:].reset_index(drop=True)
        return df_temp
    elif len(header_rows) > 1:
        head_lst = []
        for i in range(0, len(header_rows)):
            temp_lst = df_temp.iloc[i].ffill().bfill().tolist()
            head_lst.append(temp_lst)
        new_header = pd.MultiIndex.from_arrays(np.array(head_lst))
    else:
        new_header = df_temp.iloc[header_rows[-1]].ffill().bfill().tolist()

    df_temp.columns = new_header
    df_temp = df_temp.iloc[(header_rows[-1]) + 1 :]
    df_temp = df_temp.reset_index(drop=True)
    return df_temp


def parse_tb_keywords(
    tb_df, kw_spit=">>>"
):  # Extract keywords from headers (can also add LLM extraction)
    def parse_single_level_(cols, keywords):
        cols = [str(c) for c in cols]
        for col in cols:
            if kw_spit in col:
                tmp_kw = col.split(">>>")[0]
            else:  # May be first occurrence
                tmp_kw = col
            if tmp_kw not in keywords:
                keywords.append(col)
        keywords_a_level = list(set([k.strip() for k in keywords]))
        return keywords_a_level

    tb_keywords = []
    if isinstance(tb_df.columns, pd.MultiIndex):
        multi_cols = tb_df.columns
        cols_df = pd.DataFrame(
            multi_cols.tolist(),
            columns=[f"level_{i}" for i in range(multi_cols.nlevels)],
        )
        for i in range(multi_cols.nlevels):  # Extract each level as list
            level_kws = []
            level_kws = parse_single_level_(cols_df[f"level_{i}"].tolist(), level_kws)
            tb_keywords.extend(level_kws)
    else:
        tb_keywords = parse_single_level_(tb_df.columns, tb_keywords)

    # Remove duplicates while preserving column order
    tb_keywords = remove_duplicates_orderkept(tb_keywords)
    tb_keywords = [
        k
        for k in tb_keywords
        if isinstance(k, str)
        and k.strip()
        and k.strip() != "nan"
        and "Unnamed" not in k
    ]
    return ";".join(tb_keywords)


def parse_tb_contents(
    df_temp, parent_dic=None, file_name="", sheet_name="", row_header_cols=0
):
    """Parse table contents and generate HTML.

    Args:
        row_header_cols: Number of leftmost columns that are row headers (will be rendered as <th>)
    """
    if parent_dic is None:
        parent_dic = {}

    tb_res = df_temp.fillna("").infer_objects(copy=False)
    tb_strs = df2html(tb_res, row_header_cols=row_header_cols)

    tb_tree = tb_columns_to_tree(df_temp, parent_dic, file_name, sheet_name)
    tb_paths = flatten_dic2paths(tb_tree)
    return tb_paths, tb_strs


def tb_columns_to_tree(df, parent_dic, file_name, sheet_name):
    if isinstance(df.columns, pd.MultiIndex):
        # Convert MultiIndex columns to a nested dictionary (tree-like structure)
        columns = pd.DataFrame(df.columns.tolist())
        for level in range(columns.shape[1]):
            columns[level] = process_duplicate_cols(columns[level])

        new_columns = pd.MultiIndex.from_frame(columns)
        tree_structure = multiindex_to_tree(new_columns)
    else:
        # If columns are not MultiIndex, convert them to a dictionary with empty dictionaries as values
        new_columns = process_duplicate_cols(df.columns)
        tree_structure = {col: {} for col in new_columns}

    df.columns = new_columns
    if (not file_name == "") and (not sheet_name == ""):
        parent_dic[file_name][sheet_name] = tree_structure
    elif not sheet_name == "":
        parent_dic[sheet_name] = tree_structure
    elif not file_name == "":
        parent_dic[file_name] = tree_structure
    else:
        parent_dic = tree_structure
    return parent_dic


def multiindex_to_tree(multiindex):
    """Convert a MultiIndex to a tree-like nested dictionary structure."""

    def tree():
        return OrderedDict()

    root = tree()
    for keys in multiindex:
        current_level = root
        for key in keys:
            if key not in current_level:
                current_level[key] = tree()
            current_level = current_level[key]

    def convert_to_dict(d):
        if isinstance(d, OrderedDict):
            d = {k: convert_to_dict(v) for k, v in d.items()}
        return d

    return convert_to_dict(root)


def postprocess_tb(df, drop=False):
    if drop:
        # Track if index was originally a simple RangeIndex (no semantic meaning)
        # dropna(how='all') can turn RangeIndex into Int64Index by introducing gaps,
        # which would incorrectly trigger the "preserve row index" logic below.
        was_range_index = isinstance(df.index, pd.RangeIndex)

        # Drop rows where all data columns are empty (exclude _src_row from the check)
        # _src_row is always non-null, so including it would prevent any row from being dropped.
        src_row_cols = [
            c
            for c in df.columns
            if (isinstance(c, tuple) and c[0] == "_src_row") or c == "_src_row"
        ]
        if src_row_cols:
            data_cols = [c for c in df.columns if c not in src_row_cols]
            mask = df[data_cols].isna().all(axis=1)
            df = df[~mask]
        else:
            df = df.dropna(how="all")

        # If index was originally RangeIndex, re-number it to avoid gaps
        if was_range_index:
            df = df.reset_index(drop=True)

        # Drop columns that are all empty AND have no meaningful header
        # A column with a valid header should be preserved even if data is empty
        cols_to_drop = []
        for col_idx, col in enumerate(df.columns):
            # Check if all data values are NaN
            # Use iloc to avoid ambiguity when MultiIndex has duplicate tuple keys
            if df.iloc[:, col_idx].isna().all():
                # Check if the column header is meaningful
                # For MultiIndex: check if any level has a non-empty meaningful value
                # For simple index: check if the header is not None/empty
                has_meaningful_header = False
                if isinstance(col, tuple):
                    # MultiIndex column - check if any level has meaningful content
                    for level in col:
                        if (
                            level
                            and str(level).strip()
                            and str(level).strip() not in ["None", "nan", "NaN"]
                        ):
                            has_meaningful_header = True
                            break
                else:
                    # Simple column name
                    if (
                        col
                        and str(col).strip()
                        and str(col).strip() not in ["None", "nan", "NaN"]
                    ):
                        has_meaningful_header = True

                # Only drop if header is not meaningful
                if not has_meaningful_header:
                    cols_to_drop.append(col_idx)

        if cols_to_drop:
            # Use positional indices to drop columns safely (avoids duplicate MultiIndex key issues)
            cols_to_keep = [i for i in range(len(df.columns)) if i not in cols_to_drop]
            df = df.iloc[:, cols_to_keep]

        logger.debug(f"Dropped {len(cols_to_drop)} empty columns")

        # Preserve meaningful row index (header columns) as regular columns
        # Only drop=True if it's a simple RangeIndex (no semantic meaning)
        if not isinstance(df.index, pd.RangeIndex):
            # Remember if columns were MultiIndex before reset
            was_multiindex = isinstance(df.columns, pd.MultiIndex)
            n_levels = df.columns.nlevels if was_multiindex else 1

            # Avoid name collision before reset_index().
            # Two collision sources:
            #   A) An index level name, when padded into a tuple by pandas,
            #      matches an existing column.
            #   B) Multiple index levels share the same name → pandas tries
            #      to insert duplicate columns (e.g. five levels all named
            #      one merged header repeated across five padded columns.
            # Strategy: de-duplicate index.names so every level gets a unique
            # column name during reset_index, then clean up afterwards.
            existing_col_set = set(df.columns)

            def _make_padded(name):
                """Simulate the column name pandas would create for this index level."""
                if was_multiindex:
                    return (name,) + ("",) * (n_levels - 1)
                return name

            if isinstance(df.index, pd.MultiIndex):
                seen_counts = {}  # name → how many times seen so far
                deduped = []
                for n in df.index.names:
                    if n is None:
                        deduped.append(None)
                        continue
                    padded = _make_padded(n)
                    # Collision with existing column OR with a previously-seen index name
                    if padded in existing_col_set or n in seen_counts:
                        deduped.append(
                            None
                        )  # let pandas auto-name it (level_0, level_1 …)
                    else:
                        deduped.append(n)
                    seen_counts[n] = seen_counts.get(n, 0) + 1
                df.index.names = deduped
            elif hasattr(df.index, "name") and df.index.name is not None:
                padded = _make_padded(df.index.name)
                if padded in existing_col_set:
                    df.index.name = None

            df = df.reset_index()  # Converts index to columns

            # Clean up auto-generated column names like 'index', 'level_0', 'level_1'
            # For MultiIndex columns, we need to preserve the structure
            if was_multiindex:
                # Build new column tuples for the index columns
                new_cols = []
                for col in df.columns:
                    if isinstance(col, str) and (
                        col.startswith("level_") or col == "index"
                    ):
                        # Create a tuple with empty strings to match MultiIndex levels
                        new_cols.append(tuple([""] * n_levels))
                    else:
                        new_cols.append(col)
                df.columns = pd.MultiIndex.from_tuples(new_cols)
            else:
                # For simple columns
                new_cols = []
                for col in df.columns:
                    if isinstance(col, str) and (
                        col.startswith("level_") or col == "index"
                    ):
                        new_cols.append("")
                    else:
                        new_cols.append(col)
                df.columns = new_cols
        else:
            df.reset_index(drop=True, inplace=True)

    # Clean column names - preserve MultiIndex structure if present
    if isinstance(df.columns, pd.MultiIndex):
        # For MultiIndex, clean each level's values while preserving structure
        new_levels = []
        for level_idx in range(df.columns.nlevels):
            level_vals = df.columns.get_level_values(level_idx)
            cleaned = [
                str(v).replace("\n", "") if v is not None else "" for v in level_vals
            ]
            new_levels.append(cleaned)
        df.columns = pd.MultiIndex.from_arrays(new_levels, names=df.columns.names)
        # Also handle 'Unnamed' in MultiIndex
        new_levels = []
        for level_idx in range(df.columns.nlevels):
            level_vals = df.columns.get_level_values(level_idx)
            cleaned = [np.nan if "Unnamed" in str(v) else v for v in level_vals]
            new_levels.append(cleaned)
        df.columns = pd.MultiIndex.from_arrays(new_levels, names=df.columns.names)
    else:
        df.columns = [
            str(col).replace("\n", "") for col in df.columns
        ]  # Replace '\n' in column headers
        df.columns = [
            np.nan if "Unnamed" in str(col) else col for col in df.columns
        ]  # Replace Unnamed with nan
    df = df.map(
        lambda x: x.replace("\n", "") if isinstance(x, str) else x
    )  # Replace '\n' in each cell
    df = process_datetime_cells(df)
    return df


def process_datetime_cells(df):
    df = df.copy()

    def convert(x):
        if isinstance(x, (pd.Timestamp, datetime.datetime)):
            return x.strftime("%Y-%m-%d %H:%M:%S")
        return x

    return df.apply(lambda col: col.map(convert))


def process_duplicate_cols(columns):
    col_count = {}
    new_columns = []
    for col in columns:
        if col in col_count:
            new_columns.append(f"{col}>>>{col_count[col]}")
            col_count[col] += 1
        else:
            new_columns.append(col)
            col_count[col] = 1
    return new_columns


def format_tb_scope(df, num):
    if len(df) > int(num * 3 + 1):
        # Get head and tail rows
        head_df = df.head(num)
        tail_df = df.tail(num)
        # Middle portion excluding head and tail
        middle_df = df.iloc[num : len(df) - num]

        if len(middle_df) >= num:
            mid_sample_df = middle_df.sample(n=num, random_state=42)
        else:  # If middle has less than num rows, take all
            mid_sample_df = middle_df
        scope_df = pd.concat(objs=[head_df, mid_sample_df, tail_df], ignore_index=True)
    else:
        scope_df = df
    scope_df = scope_df.applymap(lambda x: str(x).strip() if pd.notnull(x) else x)
    scope_str = df2html(scope_df)
    return scope_str


def parse_xlsx(
    file_path,
    file_name,
    output_dir,
    baseurl,
    base_llm_paras=None,
    window_h=10,
    relative_root=None,
    use_precision_mode=True,
    include_hidden_sheets=False,
):
    """
    Parse Excel file and extract table content.

    Args:
        file_path: Path or URL to the Excel file
        file_name: Display name for the file
        output_dir: Directory to save extracted tables
        baseurl: Base URL for file loading
        base_llm_paras: LLM parameters for summarization
        window_h: Window size for table scope
        relative_root: Root path for relative paths
        use_precision_mode: If True, use openpyxl merged cell metadata for accurate
                           header detection. If False, use LLM/heuristic mode.
                           Default is True for better accuracy.
        include_hidden_sheets: If True, parse hidden/very-hidden sheets. Default False.

    Returns:
        DataFrame with parsed table information
    """
    time_stamp = get_str_time()
    df_list = []

    table_data = load_file_bytes(file_path, file_url=baseurl)
    table_stream = io.BytesIO(table_data)

    tb_dir = os.path.join(output_dir, "tables")
    os.makedirs(tb_dir, exist_ok=True)
    all_tb_paths = []
    exist_sheets = []

    if use_precision_mode:
        # PRECISION MODE: Use openpyxl metadata for accurate header detection
        logger.info("Using precision mode for Excel header detection")
        try:
            sheets_dict = parse_excel_structure(
                table_stream, include_hidden_sheets=include_hidden_sheets
            )
            precision_mode_active = True
        except Exception as e:
            logger.warning(f"Precision mode failed, falling back to legacy mode: {e}")
            table_stream.seek(0)  # Reset stream position
            sheets_dict = pd.read_excel(table_stream, sheet_name=None)
            precision_mode_active = False
    else:
        # LEGACY MODE: Use pandas read_excel + LLM/heuristic header detection
        sheets_dict = pd.read_excel(table_stream, sheet_name=None)
        precision_mode_active = False

    all_sheets = sheets_dict.items()

    for sheet_name, sheet_content in all_sheets:
        sheet_name = sheet_name.strip()
        if sheet_name in exist_sheets:
            sheet_name = sheet_name + str(len(exist_sheets))
        else:
            exist_sheets.append(sheet_name)

        sheet_tbs = [sheet_content]
        for tb in sheet_tbs:
            try:
                tb = postprocess_tb(tb, drop=True)
                if len(tb) == 0 or tb.empty or tb.isna().all().all():
                    continue

                # In precision mode, headers are already set by parse_excel_structure
                # In legacy mode, use LLM/heuristic header parsing
                if not precision_mode_active:
                    tb = parse_headers(tb, paras=base_llm_paras)

                # Drop _src_row column before converting to HTML/keywords
                # (_src_row is a debug column added by Excel structure parsing for cross-referencing)
                src_row_cols = [
                    c
                    for c in tb.columns
                    if (isinstance(c, tuple) and c[0] == "_src_row") or c == "_src_row"
                ]
                if src_row_cols:
                    tb = tb.drop(columns=src_row_cols)

                # Get row header column count from DataFrame attrs (set in parse_excel_structure)
                row_header_cols = tb.attrs.get("row_header_cols", 0)

                tb_paths, tb_strs = parse_tb_contents(
                    tb,
                    parent_dic={file_name: {sheet_name: {}}},
                    file_name=file_name,
                    sheet_name=sheet_name,
                    row_header_cols=row_header_cols,
                )

                # Unified LLM extraction: title + keywords + summary in one call
                # (consistent with doc_parser.py and md_parser.py)
                llm_title = None
                llm_summary = None
                tb_keywords = ""
                if base_llm_paras["summary_table"]:
                    from app.services.document_parser.txt_parser import (
                        extract_title_keywords_summary,
                    )

                    llm_title, tb_keywords, llm_summary = (
                        extract_title_keywords_summary(tb_strs, max_keywords=3)
                    )

                # Build tb_summary: table index + optional LLM summary
                table_index = f"table-{sheet_name}"
                if llm_summary:
                    tb_summary = f"{table_index}\n{llm_summary}"
                else:
                    # Fallback: use mechanical column keywords when LLM is off
                    tb_keywords_fallback = parse_tb_keywords(tb)
                    tb_summary = table_index
                    tb_keywords = tb_keywords if tb_keywords else tb_keywords_fallback

                # Use a filesystem-safe filename so LLM titles like "A/B" do not
                # accidentally create nested paths under tables/.
                effective_name = llm_title if llm_title else sheet_name
                tb_name = (
                    path_handle(
                        remove_spaces("table-" + effective_name), mode="clean_single"
                    )
                    + ".html"
                )
                soup = BeautifulSoup(tb_strs, features="html.parser")
                tb_html_str = soup.prettify()

                # Use same temp_uid for both marker and know_id (aligned with doc_parser/md_parser)
                temp_uid = gen_str_codes(tb_strs + str(sheet_name))
                tb_ref = build_chunk_ref(f"tables/{tb_name}")
                tb_bottom_content = f"{tb_ref}\nTable summary:\n{tb_summary}\nMain columns:\n{tb_keywords}"

                bottom_tokens = tokenize2stw_remove(
                    [tb_bottom_content], base_llm_paras["stopwords"]
                )

                all_tb_paths.extend(tb_paths)
                table_row = write_table_asset(
                    TableAssetInput(
                        html=tb_html_str,
                        output_dir=output_dir,
                        table_name=tb_name,
                        summary=tb_summary,
                        keywords=tb_keywords,
                        know_id=temp_uid,
                        addtime=time_stamp,
                        content=tb_bottom_content,
                        tokens=bottom_tokens,
                        length=len(tb_strs),
                    )
                )
                df_list.append(table_row.to_list())

            except KnowhereException:
                raise
            except Exception as e:
                logger.error(f"Table parsing failed: {e}")
                raise TableParsingException(
                    user_message="Failed to parse Excel table content",
                    reason="TABLE_PROCESSING_FAILED",
                    internal_message=str(e),
                    original_exception=e,
                )

    rows_builder = ParsedRowsBuilder()
    for row_values in df_list:
        rows_builder.append(
            ParsedRow(
                content=str(row_values[0]),
                path=str(row_values[1]),
                type=str(row_values[2]),
                length=int(row_values[3]),
                keywords=str(row_values[4]),
                summary=str(row_values[5]),
                know_id=str(row_values[6]),
                tokens=str(row_values[7]),
                connectto=str(row_values[8]),
                addtime=str(row_values[9]),
                page_nums=str(row_values[10]),
            )
        )
    table_df = rows_builder.to_dataframe()
    table_df = process_dup_paths_df(table_df)
    return table_df
