"""
Fragment Parser - for user-pasted text content injection

This parser handles .fragment files which represent user-pasted text content
that needs to be parsed without requiring a physical file.
"""

import os
from typing import Any, Optional

from app.services.document_parser.formats.markdown.parser import parse_md
from app.services.document_parser.orchestration.path_segment import (
    build_parser_path_segment,
)
from loguru import logger
from openai.types.chat import ChatCompletionMessageParam

from app.services.common.file_utils import path_handle
from shared.services.ai.openai_compatible_client_sync import get_openai_client


def generate_fragment_title(content: str, max_tokens: int = 30) -> Optional[str]:
    """
    Generate a concise title for fragment content using AI.

    Args:
        content: The fragment text content
        max_tokens: Maximum tokens for the generated title

    Returns:
        Generated title string
    """
    try:
        title_prompt = (
            "Generate a concise title (under 10 words) for the following text. "
            "Return ONLY the title, no quotes or explanation:\n\n"
            f"{content[:500]}"
        )
        messages: list[ChatCompletionMessageParam] = [
            {"role": "user", "content": title_prompt}
        ]
        generated_title = get_openai_client().chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            timeout=30,
        )
        if generated_title:
            return str(generated_title).strip()[:50]
        return None
    except Exception as e:
        logger.warning(f"Failed to generate fragment title via AI: {e}")
        return None


def parse_fragment(
    fragment_content: str,
    filename: Optional[str] = None,
    output_dir: Optional[str] = None,
    base_llm_paras: Optional[dict[str, Any]] = None,
    **kwargs,
):
    """
    Parse user-pasted fragment content.

    Args:
        fragment_content: The text content to parse
        filename: Optional filename, if not provided will be auto-generated
        output_dir: Base output directory
        base_llm_paras: LLM parameters for parsing

    Returns:
        tuple: (full_output_dir, relative_root, parsed_df)
    """
    if output_dir is None:
        raise ValueError("output_dir is required for fragment parsing")

    # Generate filename if not provided or is just ".fragment"
    if not filename or filename.lower() in [".fragment", "fragment", ""]:
        generated_title = generate_fragment_title(fragment_content)
        if generated_title:
            cleaned_title = path_handle(generated_title, mode="clean_single")
            if isinstance(cleaned_title, str) and cleaned_title:
                filename = f"{cleaned_title}.fragment"
            else:
                filename = f"fragment_{os.urandom(4).hex()}.fragment"
        else:
            filename = f"fragment_{os.urandom(4).hex()}.fragment"
    assert filename is not None

    logger.debug(f"Fragment filename: {filename}")

    # Build relative_root and full_output_dir
    filename_segment = build_parser_path_segment(
        filename,
        default=f"fragment_{os.urandom(4).hex()}.fragment",
    )
    relative_root = filename_segment
    full_output_dir = os.path.realpath(
        os.path.join(output_dir, filename_segment)
    )
    resolved_output_dir = os.path.realpath(output_dir)
    if (
        os.path.commonpath([resolved_output_dir, full_output_dir])
        != resolved_output_dir
    ):
        raise ValueError(
            f"Fragment output directory escaped workspace: {full_output_dir}"
        )
    os.makedirs(full_output_dir, exist_ok=True)

    logger.debug(f"Fragment relative_root: {relative_root}")
    logger.debug(f"Fragment full_output_dir: {full_output_dir}")

    # Parse fragment content using md_parser
    txt_lines = fragment_content.splitlines() if fragment_content else []
    parsed_df = parse_md(
        full_output_dir,
        source_type="md",
        md_lines=txt_lines,
        base_llm_paras=base_llm_paras or {},
        relative_root=relative_root,
    )

    return full_output_dir, relative_root, parsed_df
