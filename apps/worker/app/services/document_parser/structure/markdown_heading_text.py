"""Markdown heading text cleanup shared by heading candidates and the LLM judge."""

import re

_FULL_LINE_MD_BOLD_RE = re.compile(r"^(\*{2,3}|_{2,3})(.+)\1\s*$")


def strip_md_heading_markers(line: str) -> str:
    """Strip leading and trailing markdown heading markers from a line."""
    if not line:
        return line

    line = line.strip()
    line = re.sub(r"^#+\s*", "", line)
    # Require whitespace before trailing # so we do not mangle content like "C#".
    line = re.sub(r"\s+#+\s*$", "", line)
    return line.strip()


def detect_and_strip_md_bold(line_clean: str) -> tuple:
    """Detect and strip markdown full-line bold markers.

    Criteria for "full-line bold":
    - line_clean (with '#' prefix stripped) is entirely wrapped by **...**, __...__, or ***...***
    - Partial bolding (e.g. "- **Object Detection:** Anchor-free") is not counted.

    Args:
        line_clean: Line text with '#' prefix stripped

    Returns:
        (stripped_text, is_full_bold)
        - stripped_text: If full-line bold, returns plain text without markers; otherwise returns as-is
        - is_full_bold: Whether it is a full-line bold
    """
    if not line_clean:
        return line_clean, False

    # Match bold/bold-italic markers wrapping the entire line
    # Supports **text**, __text__, ***text***, ___text___
    m = _FULL_LINE_MD_BOLD_RE.match(line_clean)
    if m and m.group(1) in ("**", "***", "__", "___"):
        return m.group(2).strip(), True

    return line_clean, False


def clean_md_text_for_llm(line: str) -> str:
    """Strip markdown formatting markers so LLM sees semantic text only."""
    if not line:
        return line

    line = strip_md_heading_markers(line)
    line, _ = detect_and_strip_md_bold(line)
    return line.strip()
