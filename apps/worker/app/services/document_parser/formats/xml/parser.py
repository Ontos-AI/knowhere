# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportReturnType=false
"""XML document parser.

Converts .xml files into markdown-like text lines by recursively walking
the XML element tree with Python's built-in xml.etree.ElementTree, then
delegates to the standard parse_md() pipeline for hierarchy reconstruction,
heading detection, and LLM-based enrichment.

The conversion is intentionally lightweight — complex XML schemas and
namespaces are ignored in favor of text extraction. Element tag names
are used as lightweight structural hints.
"""

from xml.etree import ElementTree


def parse_xml(
    output_dir: str,
    source_type: str,
    file_path: str,
    base_llm_paras=None,
    relative_root: str | None = None,
):
    """Parse an XML file into a hierarchical document DataFrame.

    Walks the XML element tree to extract text content from all elements,
    using tag names to create markdown-like headings for structural
    containers. The extracted lines are then fed through parse_md() for
    heading detection, hierarchy reconstruction, and LLM enrichment —
    the same pipeline used by the text and HTML adapters.

    Args:
        output_dir: Full output directory path for parsed artifacts.
        source_type: Source type label (always "xml" from the adapter).
        file_path: Absolute path to the .xml file on disk.
        base_llm_paras: LLM parameter dict for summary enrichment.
        relative_root: Root path segment for hierarchical path construction.

    Returns:
        pd.DataFrame with columns [path, content, type, summary, keywords]
        representing the parsed document hierarchy.
    """
    from app.services.common.file_loading import load_file_bytes
    from app.services.document_parser.formats.markdown.parser import parse_md

    # Load and decode the XML file as UTF-8 text.
    xml_bytes = load_file_bytes(file_path, file_url="")
    xml_text = xml_bytes.decode("utf-8")

    # Convert XML document into markdown-like text lines via recursive
    # ElementTree traversal. We keep this lightweight — no schema
    # awareness, no namespace handling beyond what ElementTree provides.
    md_lines = _xml_to_md_lines(xml_text)

    # Delegate to the standard markdown parsing pipeline.
    parsed_df = parse_md(
        output_dir,
        source_type=source_type,
        md_lines=md_lines,
        base_llm_paras=base_llm_paras,
        relative_root=relative_root,
    )
    return parsed_df


def _xml_to_md_lines(xml_text: str) -> list[str]:
    """Convert XML document into markdown-like text lines.

    Parses the XML string with ElementTree, then recursively walks
    the element tree to extract text content. Container-like elements
    (based on common naming patterns) get heading-style prefixes;
    leaf elements produce plain text lines.

    Args:
        xml_text: Raw XML document as a UTF-8 string.

    Returns:
        List of text lines suitable for the parse_md() pipeline.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        # If the XML is malformed, return the raw text as fallback so
        # parse_md() can still attempt to extract meaningful content.
        return xml_text.splitlines()

    lines: list[str] = []
    _walk_xml_elements(root, lines, depth=0)

    # Strip trailing blank lines.
    while lines and not lines[-1].strip():
        lines.pop()

    return lines


def _walk_xml_elements(
    element: ElementTree.Element,
    lines: list[str],
    depth: int = 0,
) -> None:
    """Recursively walk XML elements, appending markdown-like lines.

    This is the core extraction loop. It processes each XML element:
    - Elements with only text content (leaf elements) → text line.
    - Elements with child elements → recurse, optionally emitting a
      heading line for container-like tag names.
    - Mixed content (text + children) → text line then recurse.

    Tag names that look like section/document containers (section,
    chapter, article, item, entry, record, document, part, module,
    component, chapter, topic) get a heading-style prefix based on
    nesting depth.

    Args:
        element: An xml.etree.ElementTree Element.
        lines: Accumulator list for output lines.
        depth: Current nesting depth (controls heading level).
    """
    tag = _local_name(element.tag)
    text = (element.text or "").strip()

    children = list(element)

    if not children:
        # ── Leaf element: emit tag as heading + text ───────────
        if text:
            heading = _tag_to_heading(tag, depth)
            if heading:
                lines.append(heading)
            lines.append(text)
        return

    # ── Container element: optional heading, then recurse ──────
    if text:
        # Mixed content: element has both direct text and children.
        heading = _tag_to_heading(tag, depth)
        if heading:
            lines.append(heading)
        lines.append(text)
    elif _is_container_tag(tag):
        # Pure container: emit a heading for structural navigation.
        heading = _tag_to_heading(tag, depth)
        if heading:
            lines.append(heading)

    for child in children:
        _walk_xml_elements(child, lines, depth + 1)

    # Emit tail text (text after the closing tag).
    tail = (element.tail or "").strip()
    if tail:
        lines.append(tail)


def _local_name(tag: str) -> str:
    """Strip XML namespace from a tag, returning just the local name.

    ElementTree represents namespaced tags as '{uri}localname';
    this extracts only the 'localname' portion for cleaner output.

    Args:
        tag: Full ElementTree tag string, possibly with namespace.

    Returns:
        The local part of the tag name, lowercased.
    """
    if "}" in tag:
        return tag.split("}", 1)[1].lower()
    return tag.lower()


def _is_container_tag(tag: str) -> bool:
    """Return True if the tag name looks like a document structure container.

    Heuristic based on common XML vocabulary patterns. This is
    intentionally simple — we match against a fixed set of known
    container tag names rather than trying to infer structure from
    the schema.

    Args:
        tag: Lowercased local tag name.

    Returns:
        True if the tag is recognized as a structural container.
    """
    _container_tags = frozenset({
        "section", "chapter", "article", "item", "entry",
        "record", "document", "part", "module", "component",
        "topic", "group", "block", "segment", "division",
        "body", "header", "footer", "sidebar", "content",
        "abstract", "description", "summary", "details",
    })
    return tag in _container_tags


def _tag_to_heading(tag: str, depth: int) -> str | None:
    """Convert a tag name and depth into a markdown heading line.

    Container tags at depth 0–1 get h1/h2; deeper nesting produces
    progressively lower heading levels, clamped at h6.

    Args:
        tag: Lowercased local tag name.
        depth: Nesting depth in the XML tree.

    Returns:
        A markdown heading string like '## Section Title', or None
        if the tag should not produce a heading.
    """
    if not _is_container_tag(tag):
        return None

    # Map depth to heading level: depth 0 → h1, depth 1 → h2, etc.
    # Clamp to h6 maximum.
    level = min(depth + 1, 6)
    prefix = "#" * level

    # Use a human-readable version of the tag as the heading text.
    label = tag.replace("_", " ").title()
    return f"{prefix} {label}"
