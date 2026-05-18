from __future__ import annotations

import os
import re
from typing import Any

from bs4 import BeautifulSoup
from shared.utils.chunk_refs import extract_chunk_refs
from app.services.common.file_utils import path_handle

SUMMARY_PATH_MARKERS: tuple[str, ...] = ("summary", "\u6458\u8981\u603b\u7ed3")


def find_images(folder_path: str) -> list[str]:
    """Find image files inside a folder tree."""
    image_extensions = {".png", ".jpg", ".jpeg"}
    image_files: list[str] = []

    for _, _, files in os.walk(folder_path):
        files.sort()
        for file in files:
            if os.path.splitext(file)[1].lower() in image_extensions:
                image_files.append(file)
    return image_files


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


def flatten_dic2paths(
    d: dict[str, Any],
    current_path: list[str] | None = None,
    result: list[str] | None = None,
) -> list[str]:
    """Flatten a nested dict into path strings."""
    if result is None:
        result = []
    if current_path is None:
        current_path = []

    for key, value in d.items():
        if not isinstance(key, str):
            continue
        new_path = current_path + [key]
        if isinstance(value, dict) and value:
            flatten_dic2paths(value, new_path, result)
        else:
            split_char = os.getenv("SPLIT_CHAR", "/")
            result.append(split_char.join(new_path))
    return result


def process_path_texts(path_: str, last: int = 50) -> str:
    """Normalize path text for downstream use."""
    temp_path = path_handle(path_, mode="sanitize")
    if not isinstance(temp_path, str) or temp_path == "":
        return ""
    return "_".join(temp_path.split(os.sep))[:last]


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


def traverse_dict(d: dict[str, Any], parent: str | None = None) -> list[str]:
    """Traverse a dictionary and generate description text."""
    dic_texts: list[str] = []
    for key, value in d.items():
        if value:
            child_keys = ", ".join(value.keys())
            text = f"'{key}' includes {child_keys}"
            dic_texts.append(text)
            dic_texts.extend(traverse_dict(value, key))
    return dic_texts


def restore_graph_by_paths(paths: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Rebuild a graph structure from path strings."""
    root_dict: dict[str, Any] = {}
    split_char = os.getenv("SPLIT_CHAR", "/")
    for path in paths:
        nodes = path.split(split_char)
        current_dict = root_dict
        for node in nodes:
            if node not in current_dict:
                current_dict[node] = {}
            current_dict = current_dict[node]
    dic_texts = traverse_dict(root_dict)
    return root_dict, dic_texts


def html2txt(html_text: str) -> str:
    """Convert HTML into plain text."""
    soup = BeautifulSoup(html_text, "html.parser")
    return soup.get_text()
