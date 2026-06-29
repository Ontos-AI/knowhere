"""Contract tests for the XML format parser.

Verifies that .xml files are correctly routed through the
XmlParseAdapter and produce valid ParseOutput with the expected
DataFrame contract: columns [path, content, type, summary, keywords].

Test pattern follows test_html_parser_contract.py and
test_excel_parser_contract.py.
"""

from __future__ import annotations

from pathlib import Path


def _write_contract_xml(test_xml_path: Path) -> None:
    """Write a realistic XML document to the given path for contract testing.

    The document mimics a technical specification with nested sections,
    mixed content (text + child elements), and various container types.
    """
    xml_content = """\
<?xml version="1.0" encoding="UTF-8"?>
<document>
    <title>API Specification v2.1</title>
    <abstract>
        This document defines the public API contract for the Knowhere
        document processing service, covering authentication, endpoints,
        and error handling.
    </abstract>
    <section>
        <heading>Authentication</heading>
        <paragraph>
            All API requests require a valid API key passed in the
            Authorization header using the Bearer token scheme.
        </paragraph>
        <item>
            <name>API Key Format</name>
            <description>Keys are 256-bit hex-encoded strings prefixed with "kh_".</description>
        </item>
        <item>
            <name>Key Rotation</name>
            <description>Keys may be rotated every 90 days. Old keys remain valid for a 24-hour grace period.</description>
        </item>
    </section>
    <section>
        <heading>Endpoints</heading>
        <paragraph>
            All endpoints are available under the base URL https://api.knowhereto.ai/v1.
        </paragraph>
        <item>
            <name>POST /documents/upload</name>
            <description>Upload a new document for processing. Accepts multipart form data with the file and optional metadata JSON.</description>
        </item>
        <item>
            <name>GET /documents/{id}/status</name>
            <description>Retrieve the current processing status of a document by its unique identifier.</description>
        </item>
    </section>
    <section>
        <heading>Error Handling</heading>
        <paragraph>
            The API uses standard HTTP status codes and returns error details in a consistent JSON envelope.
        </paragraph>
    </section>
</document>"""
    test_xml_path.write_text(xml_content, encoding="utf-8")


def test_xml_parser_contract_produces_valid_parse_output(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    """Verify that a .xml file is routed through the XmlParseAdapter
    and produces a ParseOutput with the expected DataFrame contract."""
    from app.services.document_parser.parse_service import checkerboard_parse_output

    xml_path = tmp_path / "api_spec.xml"
    output_root = tmp_path / "parser-output"
    _write_contract_xml(xml_path)

    # Run through the public parser seam with LLM features disabled.
    parse_output = checkerboard_parse_output(
        file_full_path=str(xml_path),
        filename="api_spec.xml",
        output_dir=str(output_root),
        internal_output_filename="api_spec.xml",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )

    full_output_dir = parse_output.output_dir
    parsed_df = parse_output.parsed_df

    # ── Output directory contract ───────────────────────────────
    assert full_output_dir.endswith("api_spec.xml"), (
        f"Expected output directory to end with 'api_spec.xml', "
        f"got: {full_output_dir}"
    )
    assert parsed_df is not None, (
        "Expected parsed_df to be non-None for XML input"
    )

    # ── DataFrame column contract ───────────────────────────────
    expected_columns = ["path", "content", "type", "summary", "keywords"]
    for col in expected_columns:
        assert col in parsed_df.columns, (
            f"Expected column '{col}' in parsed_df, "
            f"got columns: {list(parsed_df.columns)}"
        )

    # ── Content extraction ──────────────────────────────────────
    all_content = " ".join(str(c) for c in parsed_df["content"].tolist() if c)
    assert "API Specification" in all_content, (
        "Expected document title in parsed output"
    )
    assert "Authentication" in all_content, (
        "Expected section heading content in parsed output"
    )
    assert "Bearer token" in all_content, (
        "Expected paragraph text in parsed output"
    )
    assert "kh_" in all_content, (
        "Expected nested item text in parsed output"
    )

    # ── Path contract ───────────────────────────────────────────
    paths = parsed_df["path"].tolist()
    assert any(p.startswith("api_spec.xml") for p in paths), (
        f"Expected at least one path starting with 'api_spec.xml', "
        f"got paths: {paths}"
    )


def test_xml_parser_handles_malformed_xml_gracefully(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    """Verify that malformed XML is handled gracefully — the parser
    should not crash but return a best-effort parse via fallback mode."""
    from app.services.document_parser.parse_service import checkerboard_parse_output

    # Write intentionally malformed XML (missing closing tag).
    bad_xml_path = tmp_path / "bad.xml"
    bad_xml_path.write_text(
        "<root><item>Valid content</item><item>Unclosed",
        encoding="utf-8",
    )
    output_root = tmp_path / "parser-output"

    # The parser must not raise an unhandled exception on malformed input.
    parse_output = checkerboard_parse_output(
        file_full_path=str(bad_xml_path),
        filename="bad.xml",
        output_dir=str(output_root),
        internal_output_filename="bad.xml",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )

    # Should still produce a valid ParseOutput (best-effort).
    assert parse_output.parsed_df is not None, (
        "Expected parsed_df to be non-None even for malformed XML"
    )


def test_xml_to_md_lines_extracts_text_hierarchy() -> None:
    """Unit test: verify that _xml_to_md_lines correctly extracts text
    from a structured XML document and preserves the hierarchy."""
    from app.services.document_parser.formats.xml.parser import _xml_to_md_lines

    xml = """\
<?xml version="1.0"?>
<document>
    <title>Test Document</title>
    <section>
        <heading>Overview</heading>
        <paragraph>This is a test paragraph with important information.</paragraph>
    </section>
    <section>
        <heading>Details</heading>
        <item>
            <name>Item One</name>
            <description>Description of the first item.</description>
        </item>
        <item>
            <name>Item Two</name>
            <description>Description of the second item.</description>
        </item>
    </section>
</document>"""

    lines = _xml_to_md_lines(xml)

    all_text = "\n".join(lines)
    assert "Test Document" in all_text, f"Expected 'Test Document' in {lines}"
    assert "Overview" in all_text, f"Expected 'Overview' in {lines}"
    assert "important information" in all_text, (
        f"Expected paragraph text in {lines}"
    )
    assert "Item One" in all_text, f"Expected 'Item One' in {lines}"
    assert "Item Two" in all_text, f"Expected 'Item Two' in {lines}"


def test_xml_to_md_lines_handles_malformed() -> None:
    """Unit test: malformed XML should fall back to raw text lines."""
    from app.services.document_parser.formats.xml.parser import _xml_to_md_lines

    lines = _xml_to_md_lines("<root><item>text</item><broken")
    # Fallback: raw lines from the malformed string.
    assert len(lines) > 0, "Expected at least one line from fallback"


def test_local_name_strips_namespace() -> None:
    """Unit test: verify _local_name correctly strips XML namespace URIs."""
    from app.services.document_parser.formats.xml.parser import _local_name

    assert _local_name("{http://example.com/ns}section") == "section"
    assert _local_name("paragraph") == "paragraph"
    assert _local_name("{urn:oasis:names:tc:opendocument}body") == "body"
