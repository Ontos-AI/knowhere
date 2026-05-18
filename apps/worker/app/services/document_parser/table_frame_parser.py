# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportOptionalOperand=false, reportOptionalSubscript=false, reportReturnType=false, reportOperatorIssue=false, reportIndexIssue=false, reportAssignmentType=false, reportGeneralTypeIssues=false
from __future__ import annotations

import datetime
import os
import uuid
from collections import OrderedDict

import numpy as np
import pandas as pd
from app.services.document_parser.dataframe_html_renderer import df2html
from app.services.document_parser.identifiers import gen_str_codes
from app.services.document_parser.path_helpers import flatten_dic2paths
from loguru import logger

from shared.services.ai.prompt_service import build_prompt
from shared.services.ai.response_process_service import eval_response
from shared.utils.OpenAICompatibleClientSync import get_openai_client
from shared.utils.text_utils import remove_duplicates_orderkept


def parse_headers(
    table_frame: pd.DataFrame,
    paras: dict[str, object] | None = None,
    header_window: int = 5,
    smart_headers: bool = True,
) -> pd.DataFrame:
    llm_parameters = paras or {"summary_table": False}

    def parse_headers_nonsmart(candidate_frame: pd.DataFrame) -> list[int]:
        non_na_row = candidate_frame[candidate_frame.notna().any(axis=1)].head(1)
        header_id = non_na_row.index[-1] if not non_na_row.empty else None
        return list(range(header_id + 1))

    if not pd.isna(table_frame.columns).all():
        table_frame.loc[-1] = table_frame.columns
        table_frame.index = table_frame.index + 1
        table_frame = table_frame.sort_index()
        table_frame.columns = [np.nan] * table_frame.shape[1]

    if llm_parameters["summary_table"] and smart_headers:
        try:
            table_sample = table_frame.head(header_window)
            table_sample_html = df2html(table_sample)
            prompt, _temperature, _top_p, _max_tokens = build_prompt(
                task="detect-table-headers",
                texts=table_sample_html,
                query="",
                paras=llm_parameters,
            )

            messages = [
                {"role": "system", "content": "You are a helpful assistant"},
                {"role": "user", "content": prompt},
            ]

            context_task_id = gen_str_codes((str(uuid.uuid4()) + table_sample_html))

            if os.getenv("LOCAL_DEBUG", "0") != "1":
                from shared.services.redis.redis_sync_service import (
                    SyncRedisServiceFactory,
                )

                redis_service = SyncRedisServiceFactory.get_service()
                redis_service.set(
                    f"task:{context_task_id}:status",
                    "processing",
                    ttl=7200,
                )

            header_response = get_openai_client().chat_completion(
                messages=messages,
                timeout=60,
            )
            parsed_response = eval_response(header_response)
            if isinstance(parsed_response, dict):
                answer = parsed_response.get("answer", [])
            else:
                answer = parsed_response if isinstance(parsed_response, list) else []

            if not answer or len(answer) == 0:
                logger.warning(
                    "AI returned empty list, cannot identify headers, falling back to traditional mode..."
                )
                header_rows = parse_headers_nonsmart(table_frame)
            else:
                try:
                    header_id = answer[-1]
                    header_rows = list(range(header_id + 1))
                except Exception as exc:
                    logger.warning(
                        f"Failed to parse header row number: {exc}, falling back to traditional mode..."
                    )
                    header_rows = parse_headers_nonsmart(table_frame)

        except Exception as exc:
            logger.warning(
                f"Smart header parsing failed: {exc}, falling back to traditional mode..."
            )
            header_rows = parse_headers_nonsmart(table_frame)
    else:
        header_rows = parse_headers_nonsmart(table_frame)

    if len(header_rows) == 0 or (all(header is None for header in header_rows)):
        logger.warning("No valid headers detected, fallback to using row 0 as header")
        new_header = table_frame.iloc[0].ffill().bfill().tolist()
        table_frame.columns = new_header
        return table_frame.iloc[1:].reset_index(drop=True)

    if len(header_rows) > 1:
        header_levels = []
        for header_index in range(0, len(header_rows)):
            header_level = table_frame.iloc[header_index].ffill().bfill().tolist()
            header_levels.append(header_level)
        new_header = pd.MultiIndex.from_arrays(np.array(header_levels))
    else:
        new_header = table_frame.iloc[header_rows[-1]].ffill().bfill().tolist()

    table_frame.columns = new_header
    table_frame = table_frame.iloc[(header_rows[-1]) + 1 :]
    return table_frame.reset_index(drop=True)


def parse_tb_keywords(table_frame: pd.DataFrame, kw_spit: str = ">>>") -> str:
    def parse_single_level(columns: list[object], keywords: list[str]) -> list[str]:
        column_texts = [str(column) for column in columns]
        for column_text in column_texts:
            if kw_spit in column_text:
                keyword = column_text.split(">>>")[0]
            else:
                keyword = column_text
            if keyword not in keywords:
                keywords.append(column_text)
        return list({keyword.strip() for keyword in keywords})

    table_keywords: list[str] = []
    if isinstance(table_frame.columns, pd.MultiIndex):
        multi_columns = table_frame.columns
        columns_frame = pd.DataFrame(
            multi_columns.tolist(),
            columns=[f"level_{i}" for i in range(multi_columns.nlevels)],
        )
        for level_index in range(multi_columns.nlevels):
            level_keywords: list[str] = []
            level_keywords = parse_single_level(
                columns_frame[f"level_{level_index}"].tolist(),
                level_keywords,
            )
            table_keywords.extend(level_keywords)
    else:
        table_keywords = parse_single_level(table_frame.columns.tolist(), table_keywords)

    table_keywords = remove_duplicates_orderkept(table_keywords)
    table_keywords = [
        keyword
        for keyword in table_keywords
        if isinstance(keyword, str)
        and keyword.strip()
        and keyword.strip() != "nan"
        and "Unnamed" not in keyword
    ]
    return ";".join(table_keywords)


def parse_tb_contents(
    table_frame: pd.DataFrame,
    parent_dic: dict[str, object] | None = None,
    file_name: str = "",
    sheet_name: str = "",
    row_header_cols: int = 0,
) -> tuple[list[str], str]:
    if parent_dic is None:
        parent_dic = {}

    rendered_frame = table_frame.fillna("").infer_objects(copy=False)
    table_html = df2html(rendered_frame, row_header_cols=row_header_cols)

    table_tree = tb_columns_to_tree(table_frame, parent_dic, file_name, sheet_name)
    table_paths = flatten_dic2paths(table_tree)
    return table_paths, table_html


def tb_columns_to_tree(
    table_frame: pd.DataFrame,
    parent_dic: dict[str, object],
    file_name: str,
    sheet_name: str,
) -> dict[str, object]:
    if isinstance(table_frame.columns, pd.MultiIndex):
        columns = pd.DataFrame(table_frame.columns.tolist())
        for level in range(columns.shape[1]):
            columns[level] = process_duplicate_cols(columns[level])

        new_columns = pd.MultiIndex.from_frame(columns)
        tree_structure = multiindex_to_tree(new_columns)
    else:
        new_columns = process_duplicate_cols(table_frame.columns)
        tree_structure = {column: {} for column in new_columns}

    table_frame.columns = new_columns
    if (not file_name == "") and (not sheet_name == ""):
        parent_dic[file_name][sheet_name] = tree_structure
    elif not sheet_name == "":
        parent_dic[sheet_name] = tree_structure
    elif not file_name == "":
        parent_dic[file_name] = tree_structure
    else:
        parent_dic = tree_structure
    return parent_dic


def multiindex_to_tree(multiindex: pd.MultiIndex) -> dict[object, object]:
    def tree() -> OrderedDict[object, object]:
        return OrderedDict()

    root = tree()
    for keys in multiindex:
        current_level = root
        for key in keys:
            if key not in current_level:
                current_level[key] = tree()
            current_level = current_level[key]

    def convert_to_dict(value: object) -> object:
        if isinstance(value, OrderedDict):
            return {key: convert_to_dict(child) for key, child in value.items()}
        return value

    return dict(convert_to_dict(root))


def postprocess_tb(table_frame: pd.DataFrame, drop: bool = False) -> pd.DataFrame:
    if drop:
        was_range_index = isinstance(table_frame.index, pd.RangeIndex)

        source_row_columns = [
            column
            for column in table_frame.columns
            if (isinstance(column, tuple) and column[0] == "_src_row")
            or column == "_src_row"
        ]
        if source_row_columns:
            data_columns = [
                column for column in table_frame.columns if column not in source_row_columns
            ]
            mask = table_frame[data_columns].isna().all(axis=1)
            table_frame = table_frame[~mask]
        else:
            table_frame = table_frame.dropna(how="all")

        if was_range_index:
            table_frame = table_frame.reset_index(drop=True)

        cols_to_drop: list[int] = []
        for column_index, column in enumerate(table_frame.columns):
            if table_frame.iloc[:, column_index].isna().all():
                has_meaningful_header = False
                if isinstance(column, tuple):
                    for level in column:
                        if (
                            level
                            and str(level).strip()
                            and str(level).strip() not in ["None", "nan", "NaN"]
                        ):
                            has_meaningful_header = True
                            break
                elif (
                    column
                    and str(column).strip()
                    and str(column).strip() not in ["None", "nan", "NaN"]
                ):
                    has_meaningful_header = True

                if not has_meaningful_header:
                    cols_to_drop.append(column_index)

        if cols_to_drop:
            cols_to_keep = [
                index
                for index in range(len(table_frame.columns))
                if index not in cols_to_drop
            ]
            table_frame = table_frame.iloc[:, cols_to_keep]

        logger.debug(f"Dropped {len(cols_to_drop)} empty columns")

        if not isinstance(table_frame.index, pd.RangeIndex):
            was_multiindex = isinstance(table_frame.columns, pd.MultiIndex)
            column_level_count = table_frame.columns.nlevels if was_multiindex else 1
            existing_column_set = set(table_frame.columns)

            def make_padded(name: object) -> object:
                if was_multiindex:
                    return (name,) + ("",) * (column_level_count - 1)
                return name

            if isinstance(table_frame.index, pd.MultiIndex):
                seen_counts: dict[object, int] = {}
                deduped_names: list[object | None] = []
                for name in table_frame.index.names:
                    if name is None:
                        deduped_names.append(None)
                        continue
                    padded = make_padded(name)
                    if padded in existing_column_set or name in seen_counts:
                        deduped_names.append(None)
                    else:
                        deduped_names.append(name)
                    seen_counts[name] = seen_counts.get(name, 0) + 1
                table_frame.index.names = deduped_names
            elif hasattr(table_frame.index, "name") and table_frame.index.name is not None:
                padded = make_padded(table_frame.index.name)
                if padded in existing_column_set:
                    table_frame.index.name = None

            table_frame = table_frame.reset_index()

            if was_multiindex:
                new_columns = []
                for column in table_frame.columns:
                    if isinstance(column, str) and (
                        column.startswith("level_") or column == "index"
                    ):
                        new_columns.append(tuple([""] * column_level_count))
                    else:
                        new_columns.append(column)
                table_frame.columns = pd.MultiIndex.from_tuples(new_columns)
            else:
                new_columns = []
                for column in table_frame.columns:
                    if isinstance(column, str) and (
                        column.startswith("level_") or column == "index"
                    ):
                        new_columns.append("")
                    else:
                        new_columns.append(column)
                table_frame.columns = new_columns
        else:
            table_frame.reset_index(drop=True, inplace=True)

    if isinstance(table_frame.columns, pd.MultiIndex):
        new_levels = []
        for level_index in range(table_frame.columns.nlevels):
            level_values = table_frame.columns.get_level_values(level_index)
            cleaned = [
                str(value).replace("\n", "") if value is not None else ""
                for value in level_values
            ]
            new_levels.append(cleaned)
        table_frame.columns = pd.MultiIndex.from_arrays(
            new_levels,
            names=table_frame.columns.names,
        )

        new_levels = []
        for level_index in range(table_frame.columns.nlevels):
            level_values = table_frame.columns.get_level_values(level_index)
            cleaned = [np.nan if "Unnamed" in str(value) else value for value in level_values]
            new_levels.append(cleaned)
        table_frame.columns = pd.MultiIndex.from_arrays(
            new_levels,
            names=table_frame.columns.names,
        )
    else:
        table_frame.columns = [
            str(column).replace("\n", "") for column in table_frame.columns
        ]
        table_frame.columns = [
            np.nan if "Unnamed" in str(column) else column
            for column in table_frame.columns
        ]

    table_frame = table_frame.map(
        lambda value: value.replace("\n", "") if isinstance(value, str) else value
    )
    return process_datetime_cells(table_frame)


def process_datetime_cells(table_frame: pd.DataFrame) -> pd.DataFrame:
    table_frame = table_frame.copy()

    def convert(value: object) -> object:
        if isinstance(value, (pd.Timestamp, datetime.datetime)):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value

    return table_frame.apply(lambda column: column.map(convert))


def process_duplicate_cols(columns: object) -> list[object]:
    column_counts: dict[object, int] = {}
    new_columns: list[object] = []
    for column in columns:
        if column in column_counts:
            new_columns.append(f"{column}>>>{column_counts[column]}")
            column_counts[column] += 1
        else:
            new_columns.append(column)
            column_counts[column] = 1
    return new_columns


def format_tb_scope(table_frame: pd.DataFrame, num: int) -> str:
    if len(table_frame) > int(num * 3 + 1):
        head_frame = table_frame.head(num)
        tail_frame = table_frame.tail(num)
        middle_frame = table_frame.iloc[num : len(table_frame) - num]

        if len(middle_frame) >= num:
            mid_sample_frame = middle_frame.sample(n=num, random_state=42)
        else:
            mid_sample_frame = middle_frame
        scope_frame = pd.concat(
            objs=[head_frame, mid_sample_frame, tail_frame],
            ignore_index=True,
        )
    else:
        scope_frame = table_frame
    scope_frame = scope_frame.map(
        lambda value: str(value).strip() if pd.notnull(value) else value
    )
    return df2html(scope_frame)
