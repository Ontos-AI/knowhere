from __future__ import annotations

import os
import re

from shared.utils.chunk_refs import extract_chunk_refs

SUMMARY_PATH_MARKERS: tuple[str, ...] = ("summary", "\u6458\u8981\u603b\u7ed3")


def find_matches_parsing(content: str, path: str) -> str:
    """Parse table and image markers from content."""
    matches = extract_chunk_refs(content)
    match_type = "PTXT" if len(matches) == 0 else "\n".join((["PTXT"] + matches))

    split_char = os.getenv("SPLIT_CHAR", "/")
    if any(
        f"{split_char}{summary_marker}" in path
        for summary_marker in SUMMARY_PATH_MARKERS
    ):
        parent_path = path.split(split_char)[-2]
        match_type = "SUMMARY_" + parent_path + "_SUMMARY"
    return match_type


def remove_spaces(text: str, handle_punctuation: bool = False) -> str:
    """Remove spaces between Chinese chars while keeping English word spacing."""
    if handle_punctuation:
        punctuation = (
            r"""!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~，。、【】《》？；：''""（）…—-！"""
        )
        res_text = re.sub(f"[{re.escape(punctuation)}]", "", text)
    else:
        pattern = re.compile(r"([\u4e00-\u9fff])\s+|(?<=\s)([\u4e00-\u9fff])")

        def replacer(match: re.Match[str]) -> str:
            return match.group(1) or match.group(2)

        res_text = pattern.sub(replacer, text)

    res_text = re.sub(r"\s+", " ", res_text)
    return res_text.strip()
