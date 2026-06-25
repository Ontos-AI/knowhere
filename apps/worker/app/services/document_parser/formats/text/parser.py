# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportGeneralTypeIssues=false
import re

import gevent
import pandas as pd
from bs4 import BeautifulSoup
from gevent.pool import Pool as GeventPool
from loguru import logger

from shared.core.config import settings
from shared.utils.chunk_refs import CHUNK_REF_PATTERN
from app.services.common.file_loading import load_file_bytes


def clean_texts_by_form(text, form="html"):
    # try html
    soup = BeautifulSoup(text, "html.parser")
    text = soup.get_text(strip=True)
    # try other formats
    return text


def parse_texts(file_path: str, baseurl: str = "") -> list:
    """Parse text file and return lines list."""
    txt_bytes = load_file_bytes(file_path, file_url=baseurl)
    text = txt_bytes.decode("utf-8")
    txt_lines = []
    for line in text.splitlines():
        line = re.sub(r"\s", "", line)
        txt_lines.append(line)
    return txt_lines


def divide_long_contents(texts, max_threshold=None, min_threshold=None):
    from shared.core.constants import ProcessingConstants

    if max_threshold is None:
        max_threshold = ProcessingConstants.MAX_THRESHOLD
    if min_threshold is None:
        min_threshold = ProcessingConstants.MIN_THRESHOLD
    sublists = []
    current_sublist = []
    current_word_count = 0

    for text in texts:
        word_count = len(text)
        if current_word_count + word_count > max_threshold:
            sublists.append(current_sublist)
            current_sublist = [text]
            current_word_count = word_count
        else:
            current_sublist.append(text)
            current_word_count += word_count

    if current_sublist:
        sublists.append(current_sublist)

    last_count = sum(len(text) for text in sublists[-1])
    if len(sublists) > 1 and last_count < min_threshold:
        sublists[-2].extend(sublists[-1])
        sublists.pop()
    return sublists, len(sublists)


def extract_title_keywords_summary(texts, max_keywords=3, summary_len=None):
    """Extract keywords + summary for a text chunk via the unified engine (§4.1).

    Delegates to ``summarize(mode="text")``. The engine centralizes the
    ``summary-full`` prompt, deterministic language locking, JSON parsing, and
    retry. Title is no longer produced here — body content (Contract A) carries
    only summary + entities; the section title already lives on the node path.

    Args:
        texts: Input text (may include HTML tables or structured data).
        max_keywords: Maximum number of keywords to extract.
        summary_len: Maximum summary length in characters.

    Returns:
        tuple: (title, keywords_str, summary)
            - title: always None (kept for signature compatibility; §4.5 removes it)
            - keywords_str: semicolon-separated entity surface forms, or ""
            - summary: summary text, or ""
    """
    from shared.core.constants import ProcessingConstants
    from shared.services.ai.summary.engine import summarize

    if summary_len is None:
        summary_len = ProcessingConstants.SUMMARY_LEN

    result = summarize(
        mode="text",
        text=texts,
        summary_len=summary_len,
        max_keywords=max_keywords,
        usage_task="parser.text_summary",
    )
    return None, result.keywords_str(), result.summary


def postprocess_leaf_dics(
    dict_list, llm_paras, merge_key="heading", content_key="content", summary_len=None
):
    from shared.core.constants import ProcessingConstants

    if summary_len is None:
        summary_len = ProcessingConstants.POSTPROCESS_SUMMARY_LEN

    merged_dict = {}
    split_char = settings.SPLIT_CHAR or "/"
    for identifier, d in dict_list:
        identifier = split_char.join(identifier)

        if identifier in merged_dict:
            merged_dict[identifier][content_key].extend(d[content_key])
        else:
            merged_dict[identifier] = {
                merge_key: d[merge_key],
                content_key: list(d[content_key]),
            }

    merged_list = [(identifier, v["content"]) for identifier, v in merged_dict.items()]
    merge_df = pd.DataFrame(merged_list, columns=["path_identifier", "content_lst"])
    merge_df["path"] = merge_df["path_identifier"].apply(lambda x: x.split(split_char))
    merge_df = merge_df[["path", "content_lst", "path_identifier"]]

    # TODO rough dividing of contents (need more smart dividing)
    df_with_divides = pd.DataFrame(columns=["path", "content_lst", "path_identifier"])
    for i, row in merge_df.iterrows():
        if len(row["path"]) == 0:
            continue

        local_contents = row["content_lst"]
        if len(local_contents) > 0 and llm_paras["doc_type"] not in "templates":
            sublists, num = divide_long_contents(
                local_contents, max_threshold=int(3 * summary_len)
            )
        else:
            num = 0

        if num <= 1:
            df_with_divides.loc[len(df_with_divides)] = row
        else:
            head = row["path_identifier"]
            if not head:
                head = "**Preface**"
            for k in range(num):
                sub_head = (
                    head
                    + split_char
                    + head.split(split_char)[-1]
                    + " part "
                    + str(k + 1)
                )
                df_with_divides.loc[len(df_with_divides)] = {
                    "path": sub_head.split(split_char),
                    "content_lst": sublists[k],
                    "path_identifier": sub_head,
                }

    # generate summary and keywords for bottom nodes — parallel via gevent
    df_with_labels = pd.DataFrame(
        columns=["path", "content_lst", "path_identifier", "keywords", "local_summary"]
    )
    pattern = re.compile(CHUNK_REF_PATTERN)

    # Collect rows and identify which need LLM
    rows_data = []
    llm_tasks = []  # (row_index, contents4summary)
    for i, row in df_with_divides.iterrows():
        contents4summary = re.sub(pattern, "", "\n".join(row["content_lst"]))
        needs_llm = (
            len(contents4summary) > summary_len
            and llm_paras["summary_txt"]
            and (llm_paras["doc_type"] not in "templates")
        )
        rows_data.append((row, contents4summary, needs_llm))
        if needs_llm:
            llm_tasks.append((len(rows_data) - 1, contents4summary))

    # Run all LLM calls in parallel
    llm_results = {}
    if llm_tasks:
        max_concurrent = getattr(settings, "SUMMARY_LLM_MAX_CONCURRENT", 10)

        def _summarize(task):
            row_idx, text = task
            try:
                _title, kw, summary = extract_title_keywords_summary(
                    text, max_keywords=3, summary_len=summary_len
                )
                return row_idx, kw, summary
            except Exception as e:
                logger.warning(
                    f"postprocess_leaf_dics LLM failed for row {row_idx}: {e}"
                )
                return row_idx, "", ""

        pool = GeventPool(size=min(max_concurrent, len(llm_tasks)))
        greenlets = [pool.spawn(_summarize, task) for task in llm_tasks]
        gevent.joinall(greenlets)

        for g in greenlets:
            if g.value is not None:
                row_idx, kw, summary = g.value
                llm_results[row_idx] = (kw, summary)

    # Build the labeled DataFrame
    for row_idx, (row, contents4summary, needs_llm) in enumerate(rows_data):
        keywords, summary = llm_results.get(row_idx, ("", ""))
        df_with_labels.loc[len(df_with_labels)] = {
            "path": row["path"],
            "content_lst": row["content_lst"],
            "path_identifier": row["path_identifier"],
            "keywords": keywords,
            "local_summary": summary,
        }
    return df_with_labels
