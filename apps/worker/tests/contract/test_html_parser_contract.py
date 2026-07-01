"""Contract tests for the HTML format parser.

Verifies that .html and .htm files are correctly routed through the
HtmlParseAdapter and produce valid ParseOutput with the expected
DataFrame contract: columns [path, content, type, summary, keywords].

Test pattern follows test_excel_parser_contract.py — create fixture
files in tmp_path, call checkerboard_parse_output(), and assert on
the resulting ParseOutput structure.
"""

from __future__ import annotations

import os
from pathlib import Path


def _write_contract_html(test_html_path: Path) -> None:
    """Write a realistic HTML document to the given path for contract testing.

    The document includes a mix of structural elements that the HTML parser
    should handle: headings at multiple levels, paragraphs, an HTML table,
    an image reference, a list, and a code block.
    """
    html_content = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Quarterly Report</title>
    <style>body { font-family: sans-serif; }</style>
</head>
<body>
    <h1>Quarterly Financial Report</h1>

    <p>This report summarizes the financial performance for Q1 2026 across
    all business units. Overall results exceeded expectations.</p>

    <h2>Revenue by Region</h2>

    <p>The table below breaks down revenue by geographic region for the
    first quarter.</p>

    <table>
        <thead>
            <tr>
                <th>Region</th>
                <th>Q1 Revenue</th>
                <th>Growth</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td>North America</td>
                <td>$12.4M</td>
                <td>+8.2%</td>
            </tr>
            <tr>
                <td>Europe</td>
                <td>$8.1M</td>
                <td>+5.7%</td>
            </tr>
            <tr>
                <td>Asia Pacific</td>
                <td>$6.3M</td>
                <td>+12.1%</td>
            </tr>
        </tbody>
    </table>

    <h2>Key Highlights</h2>

    <ul>
        <li>Launched new enterprise product tier in February</li>
        <li>Expanded sales team by 15% across all regions</li>
        <li>Achieved 99.9% platform uptime for the quarter</li>
    </ul>

    <h3>Technical Improvements</h3>

    <p>The engineering team delivered several infrastructure upgrades
    during Q1, including the new pipeline shown below:</p>

    <pre><code>def build_pipeline():
    config = load_config("production")
    pipeline = Pipeline(config)
    pipeline.add_stage("parse")
    pipeline.add_stage("enrich")
    return pipeline</code></pre>

    <h2>Outlook</h2>

    <p>We project continued growth in Q2 2026 with the expansion into
    Latin American markets.</p>

    <img src="chart.png" alt="Revenue Projection Chart" />

    <footer>
        <p>Confidential — For internal use only</p>
    </footer>
</body>
</html>"""
    test_html_path.write_text(html_content, encoding="utf-8")


def test_html_parser_contract_produces_valid_parse_output(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    """Verify that a .html file is routed through the HtmlParseAdapter
    and produces a ParseOutput with the expected DataFrame contract.

    The parsed DataFrame must have the standard columns [path, content,
    type, summary, keywords] and must contain content extracted from the
    HTML document's structural elements (headings and paragraphs).
    """
    from app.services.document_parser.parse_service import checkerboard_parse_output

    html_path = tmp_path / "report.html"
    output_root = tmp_path / "parser-output"
    _write_contract_html(html_path)

    # Run through the public parser seam with LLM features disabled
    # so the test is fast and deterministic (same pattern as the
    # Excel parser contract test).
    parse_output = checkerboard_parse_output(
        file_full_path=str(html_path),
        filename="report.html",
        output_dir=str(output_root),
        internal_output_filename="report.html",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )

    full_output_dir = parse_output.output_dir
    parsed_df = parse_output.parsed_df

    # ── Output directory contract ───────────────────────────────
    assert full_output_dir.endswith("report.html"), (
        f"Expected output directory to end with 'report.html', "
        f"got: {full_output_dir}"
    )
    assert parsed_df is not None, (
        "Expected parsed_df to be non-None for HTML input"
    )

    # ── DataFrame column contract ───────────────────────────────
    expected_columns = ["path", "content", "type", "summary", "keywords"]
    for col in expected_columns:
        assert col in parsed_df.columns, (
            f"Expected column '{col}' in parsed_df, "
            f"got columns: {list(parsed_df.columns)}"
        )

    # ── Content extraction: verify key body text appears ─────────
    # Note: headings like "Quarterly Financial Report" are parsed into
    # the path hierarchy by parse_md(), not into the content column.
    # We check that path reflects the heading structure and content
    # contains paragraph body text.
    all_content = " ".join(str(c) for c in parsed_df["content"].tolist() if c)
    assert "summarizes the financial performance" in all_content, (
        "Expected paragraph text in parsed output"
    )

    # ── Heading hierarchy: verify paths reflect document structure ─
    paths = parsed_df["path"].tolist()
    # At least one path should start with the document root.
    assert any(p.startswith("report.html") for p in paths), (
        f"Expected at least one path starting with 'report.html', "
        f"got paths: {paths}"
    )


def test_html_parser_contract_preserves_heading_hierarchy_in_paths(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    """Verify that HTML headings are reflected in the path hierarchy
    produced by parse_md(). Headings become path segments, not content."""
    from app.services.document_parser.parse_service import checkerboard_parse_output

    html_path = tmp_path / "hier.html"
    output_root = tmp_path / "parser-output"
    _write_contract_html(html_path)

    parse_output = checkerboard_parse_output(
        file_full_path=str(html_path),
        filename="hier.html",
        output_dir=str(output_root),
        internal_output_filename="hier.html",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=True,
        stopwords=[],
    )

    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    paths = parsed_df["path"].tolist()
    # The heading structure should appear in the path column.
    assert any("Quarterly Financial Report" in str(p) for p in paths) or \
           any("Revenue by Region" in str(p) for p in paths) or \
           any("Key Highlights" in str(p) for p in paths), (
        f"Expected heading text in path hierarchy, got: {paths}"
    )


def test_htm_extension_is_routed_same_as_html(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    """Verify that .htm files are routed through the same HtmlParseAdapter
    as .html files, producing identical output structure."""
    from app.services.document_parser.parse_service import checkerboard_parse_output

    # Write identical content with .htm extension.
    htm_path = tmp_path / "summary.htm"
    output_root = tmp_path / "parser-output"
    _write_contract_html(htm_path)

    parse_output = checkerboard_parse_output(
        file_full_path=str(htm_path),
        filename="summary.htm",
        output_dir=str(output_root),
        internal_output_filename="summary.htm",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )

    parsed_df = parse_output.parsed_df
    assert parsed_df is not None, (
        "Expected non-None parsed_df for .htm input"
    )
    assert "path" in parsed_df.columns
    # Body text should be in content column; headings go into path hierarchy.
    all_content = " ".join(str(c) for c in parsed_df["content"].tolist() if c)
    assert "financial performance" in all_content, (
        "Expected body text from .htm file in content column"
    )


def test_html_parser_contract_handles_declared_non_utf8_encoding(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    """Verify that local HTML parser honors document-declared encodings."""
    from app.services.document_parser.parse_service import checkerboard_parse_output

    html_path = tmp_path / "latin1.html"
    output_root = tmp_path / "parser-output"
    html_bytes = (
        b'<!DOCTYPE html><html><head><meta charset="ISO-8859-1"></head>'
        b"<body><h1>Encoding Report</h1><p>caf\xe9 revenue summary</p></body></html>"
    )
    html_path.write_bytes(html_bytes)

    parse_output = checkerboard_parse_output(
        file_full_path=str(html_path),
        filename="latin1.html",
        output_dir=str(output_root),
        internal_output_filename="latin1.html",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )

    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    all_content = " ".join(str(c) for c in parsed_df["content"].tolist() if c)
    assert "caf\u00e9 revenue summary" in all_content, (
        f"Expected declared charset text in parsed output, got: {all_content}"
    )


def test_html_parser_traversal_safety_normalizes_paths(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    """Verify that the HTML parser safely handles path traversal attempts
    in the filename, mapping them to a flat segment within the output
    directory — same contract as the Excel parser."""
    from app.services.document_parser.parse_service import checkerboard_parse_output

    html_path = tmp_path / "notes.html"
    output_root = tmp_path / "parser-output"
    _write_contract_html(html_path)

    parse_output = checkerboard_parse_output(
        file_full_path=str(html_path),
        filename="/tmp/../notes.html",
        output_dir=str(output_root),
        internal_output_filename="../../notes.html",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )

    full_output_dir = parse_output.output_dir
    parsed_df = parse_output.parsed_df

    # Output directory must be contained within the requested root.
    assert (
        os.path.commonpath([str(output_root.resolve()), full_output_dir])
        == str(output_root.resolve())
    ), (
        f"Output directory {full_output_dir} must be within "
        f"output root {output_root}"
    )
    assert full_output_dir.endswith("notes.html"), (
        f"Expected output dir to end with 'notes.html', got: {full_output_dir}"
    )
    assert parsed_df is not None


def test_html_to_md_lines_converts_headings_correctly() -> None:
    """Unit test for the _html_to_md_lines helper — verifies that HTML
    heading tags (h1–h6) are converted to markdown heading syntax.

    This is a fast unit test that does not require the full contract
    infrastructure (no database, no Celery, no S3).
    """
    from app.services.document_parser.formats.html.document_parser import _html_to_md_lines

    html = """
    <html><body>
    <h1>Main Title</h1>
    <h2>Section A</h2>
    <p>Some paragraph text here.</p>
    <h3>Subsection A.1</h3>
    <p>More details.</p>
    <h2>Section B</h2>
    </body></html>
    """

    lines = _html_to_md_lines(html)

    assert "# Main Title" in lines, f"Expected '# Main Title' in {lines}"
    assert "## Section A" in lines, f"Expected '## Section A' in {lines}"
    assert "### Subsection A.1" in lines, f"Expected '### Subsection A.1' in {lines}"
    assert "## Section B" in lines, f"Expected '## Section B' in {lines}"
    assert "Some paragraph text here." in lines
    assert "More details." in lines


def test_html_to_md_lines_converts_tables_to_markdown() -> None:
    """Unit test: verify that <table> elements are converted to markdown
    pipe-delimited table format by the markdownify-based converter."""
    from app.services.document_parser.formats.html.document_parser import _html_to_md_lines

    html = """
    <html><body>
    <h1>Data</h1>
    <table><tr><th>Name</th><th>Score</th></tr><tr><td>Alice</td><td>95</td></tr></table>
    <p>Footer text</p>
    </body></html>
    """

    lines = _html_to_md_lines(html)

    assert "# Data" in lines
    assert "Footer text" in lines

    # markdownify converts tables to pipe-delimited markdown format.
    all_text = "\n".join(lines)
    assert "Name" in all_text, (
        f"Expected table header 'Name' in output, got: {lines}"
    )
    assert "Alice" in all_text, (
        f"Expected table cell 'Alice' in output, got: {lines}"
    )
    assert "95" in all_text, (
        f"Expected table cell '95' in output, got: {lines}"
    )


def test_html_to_md_lines_skips_script_and_style() -> None:
    """Unit test: verify that <script> and <style> elements are silently
    skipped and do not leak JavaScript/CSS into the parsed output."""
    from app.services.document_parser.formats.html.document_parser import _html_to_md_lines

    html = """
    <html><head><style>body { color: red; }</style></head>
    <body>
    <h1>Visible</h1>
    <p>Readable content.</p>
    <script>console.log("hidden");</script>
    </body></html>
    """

    lines = _html_to_md_lines(html)

    all_text = " ".join(lines)
    assert "# Visible" in lines
    assert "Readable content." in lines
    assert "color: red" not in all_text, (
        "CSS from <style> should not appear in output"
    )
    assert "console.log" not in all_text, (
        "JavaScript from <script> should not appear in output"
    )


def test_html_to_md_lines_img_produces_alt_text_only() -> None:
    """Unit test: verify that <img> tags do not produce markdown image
    references (which would be broken for single-file uploads), but
    instead emit only the alt text as an annotation."""
    from app.services.document_parser.formats.html.document_parser import _html_to_md_lines

    html = """
    <html><body>
    <p>See the chart below:</p>
    <img src="chart.png" alt="Revenue Projection" />
    <img src="logo.svg" />
    <p>End of document.</p>
    </body></html>
    """

    lines = _html_to_md_lines(html)
    all_text = " ".join(lines)

    # Alt text should be preserved as an annotation.
    assert "[Image: Revenue Projection]" in all_text, (
        f"Expected alt text annotation, got: {lines}"
    )
    # Must NOT produce markdown image syntax with src path.
    assert "![" not in all_text, (
        f"Must not produce markdown image refs, got: {lines}"
    )
    assert "chart.png" not in all_text, (
        f"Must not include unresolvable src path, got: {lines}"
    )
    # img without alt should be silently dropped.
    assert "logo.svg" not in all_text, (
        f"img without alt should produce no output, got: {lines}"
    )
