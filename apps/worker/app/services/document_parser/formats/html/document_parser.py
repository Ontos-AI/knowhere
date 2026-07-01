"""HTML document parser.

Converts .html/.htm files into Markdown-like text lines, then delegates to
the standard Markdown parser pipeline for hierarchy reconstruction and
enrichment.
"""

from typing import cast


def parse_html(
    output_dir: str,
    source_type: str,
    file_path: str,
    base_llm_paras=None,
    relative_root: str | None = None,
):
    """Parse an HTML file into a hierarchical document DataFrame."""
    from app.services.common.file_loading import load_file_bytes
    from app.services.document_parser.formats.markdown.parser import parse_md

    html_bytes = load_file_bytes(file_path, file_url="")
    md_lines = _html_to_md_lines(html_bytes)

    parsed_df = parse_md(
        output_dir,
        source_type=source_type,
        md_lines=md_lines,
        base_llm_paras=base_llm_paras,
        relative_root=relative_root,
    )
    return parsed_df


def _html_to_md_lines(html_content: str | bytes) -> list[str]:
    """Convert HTML document body into lines suitable for parse_md()."""
    converter = _SafeHtmlConverter(
        heading_style="ATX",
        bullets="-",
        strong_em_symbol="*",
    )
    # markdownify passes bytes through to BeautifulSoup for charset detection,
    # but its public type hint only accepts str.
    md_text = converter.convert(cast(str, html_content))

    lines = md_text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()

    return lines


class _SafeHtmlConverter:
    """Markdownify converter with HTML safety and single-file asset behavior."""

    def __new__(cls, **kwargs):
        from markdownify import MarkdownConverter

        converter_cls = type(
            "_SafeHtmlConverterImpl",
            (MarkdownConverter,),
            {
                "convert_script": _noop_converter,
                "convert_style": _noop_converter,
                "convert_iframe": _noop_converter,
                "convert_object": _noop_converter,
                "convert_embed": _noop_converter,
                "convert_form": _noop_converter,
                "convert_nav": _noop_converter,
                "convert_noscript": _noop_converter,
                "convert_img": _img_alt_only_converter,
            },
        )
        return converter_cls(**kwargs)


def _noop_converter(self, el, text, parent_tags):
    """Suppress unsafe or non-content elements."""
    return ""


def _img_alt_only_converter(self, el, text, parent_tags):
    """Convert <img> to alt text only, avoiding broken file references."""
    alt = el.get("alt", "").strip()
    return f"[Image: {alt}]" if alt else ""
