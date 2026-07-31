"""
Prompt Service for Document Parsing
Only contains prompts required for document parsing workflow.

Removed prompts for:
- Retrieval corpus: talk, merge-answers, judge, rerank, connect, detect-contradict
- Document Generation: gen-titles-oneoff, gen-root-titles, gen-thoughts, reason-content-layout,
                       rewrite-paras, rewrite-sentence, construct-table, reason-source
- Table Filling: filling-tb-kv, filling-tb-ck
- Other: eval-images, gen-table-query
"""

import re

from shared.services.ai.response_process_service import process_llm_history

# ──────────────────────────────────────────────────────────────────────────────
# Language detection & directive injection
# ──────────────────────────────────────────────────────────────────────────────
# Rationale: LLMs such as deepseek-v4-flash have a strong prior toward Chinese when
# summarizing numeric / structured input (financial tables, GAAP terms, etc.),
# and a soft "same language as the input" instruction is often ignored. We
# therefore detect the input language deterministically at the caller site and
# inject an EXPLICIT "Respond ONLY in <lang>" directive into the prompt.
#
# Callers pass ``paras['lang']`` with one of: 'en', 'zh', or None. When None,
# prompts fall back to the original "same language" wording.
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")


def _detect_text_language(text) -> str:
    """Return 'zh' if CJK chars dominate, 'en' if ASCII letters dominate, else 'other'.

    Uses a conservative threshold: a language wins only when it clearly
    out-counts the other. If neither is dominant, returns 'other' so that the
    caller can fall back to the legacy "same language as the input" wording.
    """
    if not isinstance(text, str) or not text:
        return "other"
    sample = text[:4000]
    cjk = len(_CJK_RE.findall(sample))
    ascii_letters = len(_ASCII_LETTER_RE.findall(sample))
    if cjk == 0 and ascii_letters == 0:
        return "other"
    if cjk >= max(8, ascii_letters * 0.5):
        return "zh"
    if ascii_letters >= max(20, cjk * 3):
        return "en"
    return "other"


def _language_directive(lang) -> str:
    """Return a strong, explicit language directive (or '' to keep defaults)."""
    if lang == "en":
        return (
            "You MUST write the ENTIRE response in ENGLISH ONLY. "
            "Do NOT use Chinese or any other language, even for individual words, "
            "keywords, titles, or punctuation marks."
        )
    if lang == "zh":
        return "你必须完全使用简体中文作答，禁止出现英文单词（专有名词除外）、日文、韩文或其他任何语言。"
    return ""


# ──────────────────────────────────────────────────────────────────────────────
# Entity-extraction & chart-numeric directives (§4.3 / §4.4)
# ──────────────────────────────────────────────────────────────────────────────
# These build the shared, GENERAL-PURPOSE instructions injected into every
# summary prompt. The type vocabulary is read from the ``ENTITY_TYPES`` config so
# it can be extended without editing prompts, and the wording deliberately avoids
# baked-in examples, sample values, or magic counts — extraction must generalize
# across arbitrary documents, not fit any one corpus.


def _entity_types() -> list[str]:
    """The configured entity type vocabulary (lower-cased, de-duplicated)."""
    from shared.core.config import settings

    raw = getattr(settings, "ENTITY_TYPES", "") or ""
    seen: dict[str, None] = {}
    for part in raw.split(","):
        label = part.strip().lower()
        if label and label not in seen:
            seen[label] = None
    return list(seen.keys())


def _entity_instruction(*, visual_layout_filter: bool = False) -> str:
    """Build the ``entities`` field instruction from the configured vocabulary.

    Returns a JSON-field directive that asks for typed entities and treats an
    empty result as valid. No entity names or counts are hard-coded; the only
    corpus-specific input is the configurable type list.
    """
    types = _entity_types()
    if types:
        type_clause = (
            "Set \"type\" to the single best-fitting label from this allowed list: "
            + ", ".join(types)
            + ". Include an entity only when its surface form truly belongs to "
            "that type; if the type is uncertain, omit the entity."
        )
    else:
        type_clause = (
            'Set "type" to a short lower-case category label you judge appropriate.'
        )
    layout_clause = ""
    if visual_layout_filter:
        layout_clause = (
            "Using visual layout, also exclude running headers and footers, "
            "page numbers, margin chrome, document or volume names used only as "
            "page chrome, section headings and outline numbering codes themselves, "
            "cross-references to other parts of the document, and generic category "
            "labels that are not specific named entities. "
        )
    return (
        '- "entities": a JSON array of the salient named entities explicitly '
        "present in the body content. Each element is an object with keys \"text\" "
        'and "type". Use the exact surface form from the content for "text". '
        f"{type_clause} {layout_clause}"
        "Do not infer, translate, or invent entities. Return an empty array [] "
        "when none are present — an empty result is valid and expected, so never "
        "force extraction."
    )


def allowed_entity_types() -> list[str]:
    """Public allowed entity-type vocabulary for post-response validation."""
    return _entity_types()


def _body_start_boundary_instruction(paras: dict | None) -> str:
    """Optional mixed TOC/body page instruction when an anchor is known."""
    text = str((paras or {}).get("body_start_text") or "").strip()
    if not text:
        return ""
    return (
        "BOUNDARY PAGE: This page mixes a Table of Contents region with body "
        "content. Body content begins at this verbatim text: "
        f'"{text}". Ignore everything before that body start. Only extract or '
        "summarize content from that point onward.\n"
    )


def build_prompt(task, texts, query, **kwargs):
    from loguru import logger

    logger.debug(
        f"build_prompt called: task={task}, texts_length={len(str(texts)) if texts else 0}"
    )
    process_llm_history(kwargs.get("paras", {}))
    logger.debug("process_llm_history completed")
    temperature = 0.1
    top_p = 0.1
    max_tokens = 2000
    prompt = ""

    # ==================== Text Processing Prompts ====================

    if task == "summary-full":
        max_tokens = kwargs["paras"]["max_tokens"]
        lang = kwargs["paras"].get("lang")
        lang_directive = _language_directive(lang)
        if lang_directive:
            lang_line = (
                "**LANGUAGE (HARD CONSTRAINT, applies to EVERY text field — "
                f"title, summary, and entity text)**: {lang_directive}"
            )
        else:
            lang_line = (
                "**First and most important**, all output must be in the "
                "**SAME LANGUAGE** as the input text"
            )

        entity_line = _entity_instruction()

        prompt = f"""
        You will receive a text passage (which may include HTML tables or
        structured/quantitative data):
        '''
        {texts}
        '''
        Extract a title, a summary, and the salient entities.
        {lang_line}

        Output requirements:
        - Respond with a single JSON object and nothing else.
        - "title": a short, descriptive title capturing the core topic. Keep it
          concise; use an empty string if the content has no clear title.
        - "summary": a faithful summary of the main content within {max_tokens}
          characters. If the input is an HTML table or other data, describe its
          structure and report its key values and extremes rather than listing
          every cell. If the content represents statistical data (charts, data
          tables, measurement results), incorporate the distinct numbers —
          extremes, totals, key values — directly into the summary sentence.
        {entity_line}
        - If the input is too short, empty, or carries no meaningful text, return
          exactly: null
        - Do not output explanations, comments, or markdown fences.
        """

    elif task == "summary-asset-linesplit":
        max_tokens = kwargs["paras"]["max_tokens"]
        lang = kwargs["paras"].get("lang")
        lang_directive = _language_directive(lang)
        if lang_directive:
            lang_line = (
                "**LANGUAGE (HARD CONSTRAINT, applies to EVERY field — "
                f"title and summary)**: {lang_directive}"
            )
        else:
            lang_line = (
                "**First and most important**, all output must be in the "
                "**SAME LANGUAGE** as the input text"
            )

        prompt = f"""
        You will receive content from a document asset (table, figure, or chart):
        '''
        {texts}
        '''
        Extract a title and a summary only. Do not extract entities or keywords.
        {lang_line}

        Output exactly TWO lines (no extra lines, no blank lines, no fences):
        Line 1: if the asset has an explicit caption or label, output it as-is; otherwise generate a short descriptive title without any punctuation
        Line 2: a faithful summary within {max_tokens} characters. If the content
        represents statistical data, incorporate the key numbers (extremes, totals,
        notable values) directly into the summary.

        If the input is empty or unreadable, output exactly two empty lines.
        Do not output JSON, markdown fences, labels, or explanations.
        """

    # ==================== Heading/Structure Prompts ====================
    elif task == "eval-headings":
        # COMPACT-input variant.  Input is pre-compressed by `compact_for_llm`
        # so that consecutive body-text rows are folded into a single
        # ``[N BODY LINES]`` placeholder row.  The LLM sees only two columns
        # (``id`` and ``heading``) — no preliminary level estimate is provided,
        # so the model assigns levels purely from structural/semantic analysis.
        temperature = 0
        top_p = 0.01
        max_depth = kwargs["paras"]["max_depth"]
        max_tokens = kwargs["paras"]["max_tokens"]
        toc_context = kwargs["paras"].get("toc_context", "")
        preceding_context = kwargs["paras"].get("preceding_context", "")

        if toc_context:
            toc_section = f"""
        ***CONFIRMED Structure: Table of Contents (TOC)***

        '''
        {toc_context}
        '''

        RULES for using the TOC (the SKELETON of the document):
        1. MUST TRUST the TOC levels as ground truth. If a candidate heading matches or
           closely corresponds to a TOC entry, you MUST assign it the same level as
           the TOC entry. Do NOT override or re-interpret the TOC's level assignment.
        2. Candidates that appear between two TOC entries should be assigned a level
           DEEPER than the TOC entry they fall under (they are sub-sections not listed
           in the TOC).
        3. A candidate that does NOT correspond to any TOC entry can only be:
           - Body text (level = -1), OR
           - A sub-section with a level deeper than its nearest TOC heading above it.
        """
        else:
            toc_section = ""

        if preceding_context:
            preceding_section = f"""
        ***PRECEDING CONTEXT (already-decided ancestor headings before this slice)***

        This slice is a CONTINUATION of the SAME document. The headings below were
        assigned in the PREVIOUS slice and their sections are still "open" — they
        flow into this slice. They are listed shallow→deep with their FINAL levels:

        '''
        {preceding_context}
        '''

        RULES for using the PRECEDING CONTEXT:
        1. Do NOT restart numbering at level 1. Continue the SAME level scale shown above.
        2. The FIRST heading you assign must be either a SIBLING of one of the open
           ancestors (same level as that ancestor) or a CHILD of the DEEPEST open
           ancestor (exactly one level deeper). It must NEVER be shallower than the
           shallowest ancestor shown unless it clearly opens a new coarse section.
        3. Apply the same parent-child continuity / no-skipping rules across the
           boundary as if the preceding headings physically preceded this slice.
        """
        else:
            preceding_section = ""

        if preceding_context:
            rule_5 = """Rule 5 — Continue the level scale from PRECEDING CONTEXT (do NOT reset to 1):
            This slice continues an earlier slice of the same document, so the
            shallowest heading here is NOT necessarily level 1. Align the first
            heading with the open ancestor levels in PRECEDING CONTEXT (sibling =
            same level, sub-section = one level deeper) and keep every level on
            that same scale."""
        else:
            rule_5 = """Rule 5 — Normalise to start at level 1:
            The shallowest (the most coarse granularity) heading found MUST be assigned level 1."""

        prompt = f"""
        You are a document structure auditing expert. The input you receive is a
        COMPACT skeleton of a document. Body-text lines have already been collapsed
        for you so that every row is one of two kinds:

        1) HEADING CANDIDATE — ``id`` is an integer, ``heading`` is the candidate
           text. These — and ONLY these — are the rows you can evaluate.

        2) PLACEHOLDER — ``id`` is ALWAYS a range "start-end" (for a single-line
           it is "N-N", e.g. "56-56"); ``heading`` is "[N BODY LINES]"
           where N is the number of body lines folded here.
           Placeholders are positional markers that tell you how many body lines sit
           between adjacent candidates. Use them as context ONLY.

        Data to be evaluated:
        '''
        {texts}
        '''

        {toc_section}
        
        {preceding_section}

        ***Hard rules about placeholders***
        - Placeholders are NEVER candidates. Do not output them.
        - Every ``id`` in your output MUST be a single integer; never emit an id containing a hyphen.
        - Use N in ``[N BODY LINES]`` as a "section bulk" signal when applying rules below (Rules 2 and 3 in particular).

        ***Process in TWO steps:***

        **STEP 1 — Global Pattern Scan (CANDIDATES ONLY)**
        Enumerate every distinct numbering / structural / semantic granularity pattern that
        appears on candidate rows and signals hierarchy depth, for example:
        - Decimal numbering: "1.", "1.1", "1.1.1" → depth increases with dot count
        - Enumeration styles: "一、" "（一）" "1、" "①" "1 " → shallower to deeper with increasing numbers
        - Chapter/section keywords: "Chapter X", "Part X", "第X章", "第X节"
        - Clear semantic granularities or groups of themes
        - Upper case / lower case differences
        Rank these patterns from shallowest to deepest to form a pattern → level mapping.
        Placeholder rows MUST NOT influence this scan.

        **STEP 2 — Assign a level to every candidate (rules in priority order)**
        Your task is to determine each candidate's heading level from scratch
        based on its text, context, and the patterns discovered in STEP 1.

        Rule 1 — Parent-child continuity and no level skipping:
            A heading, compared to candidates before it, may stay at the same level,
            or go ONE level deeper than its nearest valid ancestor heading.
            However, jumps such as level 1 → level 3 are **always invalid**.

        Rule 2 — Semantic headings detection (NO numbering pattern):
            A candidate WITHOUT any structural/numbering marker can still be a
            heading, but ONLY when ALL of the following hold:
            a) The text is short and title-like — no sentence-ending punctuation.
            b) In the input sequence it is IMMEDIATELY followed by a
                placeholder ``[N BODY LINES]``, or by another candidate with finer granularity.
                This is the "section bulk" signal — the row introduces a body block or a subsection group.

        Rule 3 — Body text demotion (candidate → -1):
            Demote a candidate to level = -1 when it clearly does NOT serve as a
            section title. Strongest demotion cues:
            - The text is an isolated fragment, data value, or caption-like snippet (e.g. "Table x", "Figure x", "Table/Figure").
            - The text has sentence-ending punctuation or is clearly prose.

        {rule_5}

        ***Output requirements***
        - Output MUST be a [JSON array] only.
        - Include ONLY candidate rows you judge to be headings (level >= 1).
        - NEVER emit a placeholder row. Every ``id`` field MUST be a single integer (no hyphen).
        - Each element must contain these fields in order:
            - "id": original line number (integer)
            - "level": the corrected heading level (integer from 1 to {max_depth})

        ***Format requirements***
        - Output only valid JSON — do not add markdown fences (no ```json).
        - Do not add any explanations, comments, control characters, or descriptive texts.
        """

    # ==================== Merge-Group Pre-pass Prompt ====================

    elif task == "eval-merge-groups":
        # Focused single-question prompt: ONLY decides merge vs. keep for
        # groups of consecutive heading candidates (no body text between them).
        # Does NOT assign hierarchy levels — that is left to the main LLM call.
        temperature = 0
        top_p = 0.01
        max_tokens = kwargs["paras"].get("max_tokens", 800)
        prompt = f"""
        You are a PDF heading reconstruction expert.

        A PDF renderer sometimes splits a single long heading title across multiple
        consecutive lines. You will receive a numbered list of groups. Each group
        contains 2–6 consecutive heading candidate lines from a PDF with NO body
        text between them.

        Your ONLY task: for each group, decide whether the lines should be MERGED
        into one single heading, or kept as SEPARATE headings in a parent-child
        relationship.

        **MERGE when ALL hold:**
        1. Reading the lines in sequence produces ONE grammatically complete,
           natural-sounding title — no missing words, no awkward break.
        2. The first line alone is grammatically INCOMPLETE as a standalone title
           (e.g. ends with a possessive "'s", a preposition "of / for / and",
           a conjunction, or is otherwise a dangling fragment).
        3. No semantic gap: every subsequent line is a direct lexical extension
           of the first, not a new sub-topic.

        **KEEP SEPARATE when ANY hold:**
        - The first line is already a complete, self-contained title on its own.
        - Subsequent lines introduce a different topic or finer sub-topic.
        - Lines form a clear parent-heading → child-heading sequence.
        - **Any subsequent line begins with a numeric or ordinal prefix** such as
          `01`, `1.`, `(1)`, `①`, `一、`, `第一` — these are numbered sub-items,
          never continuation fragments of the preceding heading.

        **Generic linguistic signals that indicate MERGE:**
        - Line ends with a possessive ("Company's", "Board's") — demands a noun phrase.
        - Line ends with a preposition ("of", "for", "under", "and") — phrase is incomplete.
        - Line ends mid-adjective or mid-noun phrase that continues on the next line.

        Groups to evaluate:
        {texts}

        Output a JSON array — one object per group, in the SAME ORDER as the input:
        [{{"group": 1, "merge": true}}, {{"group": 2, "merge": false}}, ...]

        Output ONLY valid JSON. No markdown fences, no explanations.
        """

    # ==================== TOC Heading Evaluation Prompts ====================

    elif task == "eval-toc-headings":
        temperature = 0
        top_p = 0.01
        max_depth = kwargs["paras"]["max_depth"]
        max_tokens = kwargs["paras"]["max_tokens"]

        prompt = f"""
        You are a document structure auditing expert specializing in Table of Contents (TOC) analysis. You will receive a Markdown table representing a TOC extracted from a document. Each row is a TOC entry, including:
        1. id column: line number (integer)
        2. heading column: the TOC entry text content
        3. level column: preliminary estimated level (may be inaccurate or "Not Sure")

        Data:
        '''
        {texts}
        '''

        ***Critical Context***:
        This is a Table of Contents (TOC), NOT body text. In a TOC:
        - ALL rows are heading entries pointing to document sections
        - There is NO body text in a TOC - every line represents a chapter, section, or subsection title
        - Level -1 (body text marker) is NOT applicable in TOC context

        ***Your Task***:
        Analyze the hierarchical structure of this TOC and assign the correct level (1 to {max_depth}) to each entry.

        ***Hierarchy Rules***:
        1. Top-level chapters (e.g., "Chapter 1", "Part I", "一、", "第一章", Roman numerals like "I.", "II.") should be level 1
        2. Numbered items under a chapter (e.g., "1.", "2.", "1.1", "(1)") are typically level 2 or deeper
        3. Sub-items with deeper numbering (e.g., "1.1.1", "(a)", "①") indicate level 3 or deeper
        4. Levels between consecutive entries cannot skip (e.g., jumping from level 1 to level 3 is invalid)
        5. When entries share the same numbering pattern, they should have the same level
        
        ***Output Requirements***:
        - Output MUST be a JSON array only
        - Each element must contain exactly these fields in order:
            - "id": original line number (integer)
            - "level": the corrected level for that entry (integer from 1 to {max_depth})
        - DO NOT use level -1 (this is a TOC, not body text)
        - If uncertain about a level, estimate based on the numbering pattern and context

        ***Format Requirements***:
        - Output only valid JSON, no markdown code fences (no ```json)
        - No escaped newlines or control characters
        - No explanations, comments, or descriptive text
        """

    # ==================== Page-Memory Native Hierarchy Prompts ====================

    elif task == "page-memory-vlm-page":
        temperature = 0
        top_p = 0.01
        paras = kwargs.get("paras", {}) or {}
        max_tokens = paras.get("max_tokens", 800)
        scan_direction = paras.get("scan_direction", "top_to_bottom_left_to_right")
        boundary_line = _body_start_boundary_instruction(paras)
        entity_line = _entity_instruction(visual_layout_filter=True)

        if "right_to_left" in scan_direction:
            reading_order_upper = "TOP-TO-BOTTOM, RIGHT-TO-LEFT"
            column_order = "right to left (i.e. finish the right column before starting the left column)"
        else:
            reading_order_upper = "TOP-TO-BOTTOM, LEFT-TO-RIGHT"
            column_order = "left to right (i.e. finish the left column before starting the right column)"

        prompt = f"""\
        You are annotating one PDF page for a document memory system. In one
        pass, independently:
        1. extract document-outline-level headings;
        2. summarize the page;
        3. extract typed entities.

        Summary and entity extraction MUST NOT change which titles qualify.
        If no text qualifies as a section heading, return an empty titles list.

        {boundary_line}
        READING ORDER:
        This page may contain one or more readable columns.
        Within each column, read from top to bottom.
        Between columns, read from {column_order}.
        Return every qualifying heading on this page in that reading order.

        Return one strict JSON object with exactly these keys:
        {{
        "titles": [
            {{
            "text": "<exact verbatim heading>",
            "is_in_table": <boolean>,
            "is_in_header_footer": <boolean>
            }}
        ],
        "summary": "<concise summary of what this page contains>",
        "entities": [{{"text": "<surface form>", "type": "<type>"}}]
        }}

        ═══ TITLE RULES (CRITICAL) ═══
        For EVERY extracted heading, you MUST accurately evaluate these two flags:
        1. is_in_table (boolean): Set to `true` if the text is ANYWHERE inside a table.
        2. is_in_header_footer (boolean): Set to `true` if the text is located in the top margin (header) or bottom margin (footer) of the page.

        ═══ WHAT TO EXTRACT ═══

        Only extract text that satisfies ALL three criteria:

        1. HEADING FUNCTION (primary — must be true):
        The text serves as a TITLE for the body content following it according to the reading order.
        If you removed this text, the following body content would lose its topic label.

        2. STANDALONE LINE (must be true):
        The text occupies its own line, clearly separated from surrounding
        body paragraphs. It is NOT inside a table, NOT part of a list, and NOT embedded within a sentence.

        3. VISUAL DISTINCTION (supporting):
        The text is visually set apart from body text — larger font, bold,
        centered, extra vertical spacing, or wrapped in a distinctive background color block.

        Return titles in {reading_order_upper} order. Text must be EXACT verbatim.

        ═══ WHAT TO EXCLUDE (critical — read carefully) ═══

        1. TABLE CONTENT — Any text that is part of a table. If the
        text is surrounded by grid lines, borders, or cell boundaries, it is table
        content and MUST BE EXCLUDED. This applies even when the text is bold,
        large, or spans a merged cell. Typical examples include:
        - Column headers, row category labels, merged-cell group labels
        - Any label inside a tabular layout, regardless of font size or boldness

        2. PAGE PERIPHERY — Text in margins or corners of the page:
        organization/document names repeated as running headers, page numbers,
        book/volume titles used as running headers or footers.

        3. INLINE TEXT — bullet list items, numbered clauses, or text that continues a paragraph.
        These are content items, not section headings, even if bold.

        4. CAPTIONS — Figure/table captions, footnotes.

        ═══ IMPORTANT ═══
        1. Many pages consist entirely of tables, numbered clauses, or appendix forms.
        These pages have NO qualifying headings. Return {{"titles": []}} for them.

        2. COMPLETENESS — Each heading must be the full heading as printed:
        include its complete numbering/code AND exact title text; do not
        abbreviate, truncate, or omit either part. 
        
        3. If a single heading wraps across more than one lines, merge the lines into ONE heading;
        never split one wrapped heading into multiple results.

        ═══ SUMMARY AND ENTITIES ═══
        - "summary": describe the main content visible on the page in a few
          sentences, in the same language as the page. If the page contains a
          table, state its topic and key columns; if it contains a figure or
          chart, describe what it depicts and any standout values.
        {entity_line}

        Return ONLY the JSON object, with no markdown fences or extra text.
        """

    elif task == "page-memory-vlm-titles":
        temperature = 0
        top_p = 0.01
        paras = kwargs.get("paras", {}) or {}
        max_tokens = paras.get("max_tokens", 800)
        scan_direction = paras.get("scan_direction", "top_to_bottom_left_to_right")
        boundary_line = _body_start_boundary_instruction(paras)

        if "right_to_left" in scan_direction:
            reading_order_upper = "TOP-TO-BOTTOM, RIGHT-TO-LEFT"
            column_order = "right to left (i.e. finish the right column before starting the left column)"
        else:
            reading_order_upper = "TOP-TO-BOTTOM, LEFT-TO-RIGHT"
            column_order = "left to right (i.e. finish the left column before starting the right column)"

        prompt = f"""\
        You are annotating one PDF page for a document memory system.
        Extract document-outline-level headings only. Do not summarize the page
        and do not extract entities.

        If no text qualifies as a section heading, return an empty titles list.

        {boundary_line}
        READING ORDER:
        This page may contain one or more readable columns.
        Within each column, read from top to bottom.
        Between columns, read from {column_order}.
        Return every qualifying heading on this page in that reading order.

        Return one strict JSON object with exactly this key:
        {{
        "titles": [
            {{
            "text": "<exact verbatim heading>",
            "is_in_table": <boolean>,
            "is_in_header_footer": <boolean>
            }}
        ]
        }}

        ═══ TITLE RULES (CRITICAL) ═══
        For EVERY extracted heading, you MUST accurately evaluate these two flags:
        1. is_in_table (boolean): Set to `true` if the text is ANYWHERE inside a table.
        2. is_in_header_footer (boolean): Set to `true` if the text is located in the top margin (header) or bottom margin (footer) of the page.

        ═══ WHAT TO EXTRACT ═══

        Only extract text that satisfies ALL three criteria:

        1. HEADING FUNCTION (primary — must be true):
        The text serves as a TITLE for the body content following it according to the reading order.
        If you removed this text, the following body content would lose its topic label.

        2. STANDALONE LINE (must be true):
        The text occupies its own line, clearly separated from surrounding
        body paragraphs. It is NOT inside a table, NOT part of a list, and NOT embedded within a sentence.

        3. VISUAL DISTINCTION (supporting):
        The text is visually set apart from body text — larger font, bold,
        centered, extra vertical spacing, or wrapped in a distinctive background color block.

        Return titles in {reading_order_upper} order. Text must be EXACT verbatim.

        ═══ WHAT TO EXCLUDE (critical — read carefully) ═══

        1. TABLE CONTENT — Any text that is part of a table. If the
        text is surrounded by grid lines, borders, or cell boundaries, it is table
        content and MUST BE EXCLUDED. This applies even when the text is bold,
        large, or spans a merged cell. Typical examples include:
        - Column headers, row category labels, merged-cell group labels
        - Any label inside a tabular layout, regardless of font size or boldness

        2. PAGE PERIPHERY — Text in margins or corners of the page:
        organization/document names repeated as running headers, page numbers,
        book/volume titles used as running headers or footers.

        3. INLINE TEXT — bullet list items, numbered clauses, or text that continues a paragraph.
        These are content items, not section headings, even if bold.

        4. CAPTIONS — Figure/table captions, footnotes.

        ═══ IMPORTANT ═══
        1. Many pages consist entirely of tables, numbered clauses, or appendix forms.
        These pages have NO qualifying headings. Return {{"titles": []}} for them.

        2. COMPLETENESS — Each heading must be the full heading as printed:
        include its complete numbering/code AND exact title text; do not
        abbreviate, truncate, or omit either part.

        3. If a single heading wraps across more than one lines, merge the lines into ONE heading;
        never split one wrapped heading into multiple results.

        Return ONLY the JSON object, with no markdown fences or extra text.
        """

    elif task == "page-memory-text-page":
        temperature = 0
        top_p = 0.01
        paras = kwargs.get("paras", {}) or {}
        max_tokens = paras.get("max_tokens", 600)
        boundary_line = _body_start_boundary_instruction(paras)
        entity_line = _entity_instruction(visual_layout_filter=False)
        prompt = f"""\
        You are summarizing one PDF page from its extracted body text for a
        document memory system. The input is plain text only; you cannot see
        visual layout, so do not claim to identify headers, footers, or margin
        chrome by position.

        {boundary_line}Page text:
        '''
        {texts}
        '''

        Return one strict JSON object with exactly these keys:
        {{
        "summary": "<concise summary of what this page contains>",
        "entities": [{{"text": "<surface form>", "type": "<type>"}}]
        }}

        Rules:
        - "summary": describe the main body content of this page in a few
          sentences, in the same language as the text. If the page is mostly a
          table, state its topic and key columns; if it describes a figure or
          chart, summarize what the text says about it.
        {entity_line}
        - Return ONLY the JSON object, with no markdown fences or extra text.
        """

    elif task == "page-memory-hierarchy":
        temperature = 0
        top_p = 0.01
        max_depth = kwargs["paras"].get("max_depth", 6)
        max_tokens = kwargs["paras"].get("max_tokens", 2000)
        coarse_context = kwargs["paras"].get("coarse_context", "")
        coarse_section = f"""
Confirmed coarse parent section (the scope of this subtree):
'''
{coarse_context}
'''

The input candidates already lie strictly INSIDE this coarse parent. The parent's
own title and the next coarse sibling title (if any) have already been removed.
Do NOT restate or invent those coarse endpoint titles.

Level 1 means the first heading level under this coarse parent. Nest deeper
headings relative to that parent only.

""" if coarse_context else ""
        prompt = f"""
You are constructing a fine-grained document hierarchy for ONE already-bounded
PDF segment. The input rows are NOT raw body text. They are clean title
candidates observed directly from page screenshots by a VLM, then trimmed to
the interior of one coarse TOC leaf.

Your task:
- Assign a relative hierarchy level to each real section/table/form heading.
- Level 1 means top-level under the confirmed coarse parent, level 2 is its
  child, etc.
- Preserve all legitimate sibling headings. Consecutive same-level headings are
  normal and MUST NOT be demoted just because no body text appears between rows.
- Use page order as reading order. Numbering and structural pattern are the
  primary signals for level assignment.

{coarse_section}

Input rows:
'''
{texts}
'''

Rules:
1. Trust structural numbering patterns first:
   - "1", "2", "3" style headings at the same granularity are siblings.
   - "3.1" is a child of "3"; "3.2.1" is a child of "3.2".
   - "附录 A/B/C" are top-level siblings inside the segment unless the parent
     context says otherwise.
   - "表/附表" entries under an appendix are usually children of that appendix.
2. Do not skip levels. A child can be at most one level deeper than its nearest
   valid parent.
3. Filter only obvious noise:
   - duplicate/repeated variants of the same heading on adjacent rows;
   - TOC/index headings such as "Contents", "目录", "目次";
   - front matter such as "前言" when it is outside the segment's body outline.
4. When two rows are near-duplicates, keep the clearer/more complete one and
   omit the duplicate from output.
5. Do not invent headings. Only return ids that exist in the input.

Output requirements:
- Output ONLY a valid JSON array. No markdown fences, no explanations.
- Include each retained heading as:
  {{"id": <integer>, "level": <integer from 1 to {max_depth}>}}
- Omitted ids are treated as filtered noise.
"""

    elif task == "transcribe":
        # Unified OCR primitive (§4.2): replaces the former ``page-memory-vlm-ocr``
        # and ``ocr-image`` prompts. Transcribes page/image body text verbatim.
        temperature = 0
        top_p = 0.01
        max_tokens = kwargs.get("paras", {}).get("max_tokens", 1500)
        prompt = """\
        You are transcribing a document page or image for a document memory
        system. Extract the body text as faithfully as possible.

        Return strict JSON with exactly this key:
        {
        "text": "<verbatim body text>"
        }

        Rules:
        - Preserve the ORIGINAL LANGUAGE of the text; do not translate.
        - Preserve the reading order (top-to-bottom, left-to-right).
        - Transcribe tables row by row using a simple readable layout.
        - Do NOT add commentary, translation, or summary — transcription only.
        - Omit pure decorative running headers/footers and page numbers.
        - If there is no readable text, return {"text": ""}.
        - Return ONLY the JSON object, no markdown fences or extra text.
    """

    elif task == "page-memory-asset-detect":
        temperature = 0
        top_p = 0.01
        max_tokens = kwargs.get("paras", {}).get("max_tokens", 1200)
        grid_size = kwargs.get("paras", {}).get("grid_size", 1000)
        
        prompt = f"""\
        You are a precise document layout detector. The attached image is a single
        rendered PDF page.

        Find visually distinct tables and figures that should become reusable
        document assets. Locate them only - do NOT summarize, transcribe full
        content, extract keywords, or read data values. Return strict JSON:
        {{
        "regions": [
            {{
            "kind": "table|figure",
            "bbox": [x1, y1, x2, y2],
            "title": "<short asset title or its visible caption/label, empty string if none>",
            "confidence": 0.0
            }}
        ]
        }}

        Coordinate system:
        - Treat the page image as a {grid_size}x{grid_size} grid.
        - Origin is the top-left corner.
        - bbox values must be integers in [0, {grid_size}].
        - bbox must tightly include the whole asset: its title, caption, legend,
        axes, labels, table headers, and footnotes that belong to that asset.
        - Exclude surrounding body paragraphs, page headers, page footers, and page
        numbers.

        Rules:
        - "table": ONLY a conventional data table with visible grid and strongly regular cell alignment.
        Require ALL of:
          (1) an explicit header row and/or header column that labels the fields.
          (2) one intact axis-aligned rectangular footprint: all four corners of
          the table body are present, every data row spans that full width, and
          the cell grid fills the rectangle without cutouts, protruding corner
          panels, or L-shaped outlines.
        typical table cases: forms, financial tables, data rows with field headers.

        - Do NOT mark as "table": process boards, flowchart-like matrices, cards
        connected by arrows, multi-column visual layouts, comparison panels, or
        any region whose meaning depends on icons/arrows/color blocks rather than
        plain headered cells. Those must be "figure".
        - If unsure whether it meets this bar, prefer "figure".

        - "figure": any non-table visual asset - charts, plots, diagrams,
        flowcharts, architecture drawings, schematics, embedded images, and the
        table-like visuals excluded above.
        - Prefer one bbox for the whole figure. When multiple visual parts clearly
        form one composition (shared caption or a multi-panel explanation of the
        same concept/process), return them as a single figure, not separate images.
        - Treat a flowchart or process diagram as one figure, including its nodes,
        edges, labels, and legend when they belong together.

        - Do NOT extract page backgrounds, watermarks, stamps, or decorative underlays.
        - Do NOT extract small logos, icons, bullets, or other scattered decorative
        marks that are not standalone informative figures.
        - Do NOT extract ornamental digits/letters placed before a heading title as figures.

        - Do NOT mark ordinary paragraphs, bullet lists, title blocks, or loose multi-line text as tables.
        - Do NOT split a single coherent table or figure into sub-parts.

        - "title" is one short label only (single line). Do not duplicate it into
        other fields and do not write a summary. Use an empty string when there is
        no visible title or caption.
        - Use confidence 0.0-1.0. Only include assets you can localize.
        - If there are no qualifying assets, return {{"regions":[]}}.
        - Return ONLY the JSON object, no markdown fences or explanations.
        """

    elif task == "page-memory-table-continuity":
        temperature = 0
        top_p = 0.01
        max_tokens = 200
        prompt = """\
You are a document table analysis expert. You are given two HTML table fragments from consecutive PDF pages. Both tables have the same column count.

Your task: determine whether Table B is a continuation of Table A (split across pages) or an independent table.

[TABLE A - header rows (from the beginning of the table)]
{header_rows}

[TABLE A - last rows (from the end of the page)]
{tail_rows}

[TABLE B - first rows (from the beginning of the next page)]
{head_rows}

Step 1 - Continuation check:
Table B is a NEW independent table if ANY of:
- It opens with a standalone title or caption spanning all columns
- Row numbering or indexing restarts rather than continuing from Table A
- The column semantics are structurally different from Table A

Table B is a CONTINUATION if:
- Row numbering or content logically follows from Table A's last rows
- The column structure and semantics are consistent

Step 2 - Repeated header detection (only if continuation):
Paginated tables sometimes reprint column headers at the top of each new page. Compare the first rows of Table B against Table A's header rows provided above. Count how many consecutive leading rows in Table B are repeated column headers rather than new data rows. Consider that headers may span multiple rows when columns have nested or grouped labels.

Return ONLY strict JSON, no markdown fences:
{{"is_continuation": true/false, "header_rows_to_skip": 0, "reason": "<one sentence>"}}

header_rows_to_skip: integer, the number of leading rows in Table B that duplicate the table header and should be removed before merging. Set to 0 if Table B starts directly with data rows.
"""
        tail_rows = kwargs.get("paras", {}).get("tail_rows", "")
        head_rows = kwargs.get("paras", {}).get("head_rows", "")
        header_rows = kwargs.get("paras", {}).get("header_rows", "")
        prompt = prompt.format(
            tail_rows=tail_rows,
            head_rows=head_rows,
            header_rows=header_rows,
        )

    # ==================== Image Processing Prompts ====================

    elif task == "summary-images":
        temperature = 0.1
        max_tokens = int(kwargs["paras"]["max_tokens"] * 1.2)
        if texts.strip():
            img_context = (
                f"- Context for this image: [{texts}]. You may use it to "
                "disambiguate, but describe only what the image actually shows."
            )
        else:
            img_context = ""

        prompt = f"""
        You will receive a single image extracted from a document (it may be a
        chart, a table, a diagram, a credential/form, a technical drawing, a
        photo, or any other visual asset). Extract the most useful information it
        carries and return one strict JSON object with these keys:

        {{
        "title": "<the asset's own caption or label>",
        "summary": "<what the asset shows and its key information>"
        }}

        How to summarize, by what the image actually is (decide internally; do not
        output the type):
        - Quantitative chart or data table: state what is measured, the categories
          or time range covered, and the standout values — highs, lows, totals, and
          trends. Incorporate the distinct numbers directly into the summary.
        - Diagram / flow / architecture: name the main components and how they
          relate, and the overall flow or hierarchy.
        - Credential / form / technical drawing: report the visible fields and
          their values exactly as shown (identifiers, dates, codes, dimensions,
          specifications).
        - Photo or other: describe the primary subject, any visible text or
          signage, and contextual cues.

        Field rules:
        - "title": the asset's own visible caption or label. Use an empty string
          when the asset has none — do not invent one.
        - "summary": faithful to the image, within about {max_tokens} characters,
          in the SAME LANGUAGE as any text visible in the image (use English when
          the image has no text). If the content represents statistical data,
          incorporate the key numbers (extremes, totals, notable values) directly
          into the summary sentence.
        - Do not extract entities or keywords. Summary is the only content field.
        - If the image is blank, unreadable, or carries no meaningful content,
          return exactly: null
        {img_context}
        - Return ONLY the JSON object, with no prefixes, markdown fences, or extra
          commentary.
        """

    elif task == "judge-image-type":
        temperature = 0.1
        prompt = """
        You will receive an image. Your task is to determine whether the image is primarily text-based or image-based. Note:
        - Text-based images include posters, display boards, scanned documents, etc.
        - All images except those with rich text content are considered image-based
        - Output strictly in JSON dictionary format with key "answer" and value can only be "text" or "image"
        - Do not return any additional explanations or descriptions
        """

    elif task == "atlas-page-info":
        temperature = 0.1
        max_tokens = 300
        prompt = """
        You will receive a scanned page from an engineering atlas (drawing collection).
        Your task is to extract the atlas number, atlas name, and page label from the title block (info bar), then format the output EXACTLY as shown below.

        Steps:
        1. FIRST: Find the title block / info bar (usually at the bottom-right corner or bottom edge of the page).
           - Extract:
             a) Atlas number: a code with letters and digits, which may include hyphens
             b) Atlas name: the drawing collection name, whether shown in Chinese or English
             c) Page label: the page number or page tag shown in the title block
           - Output EXACTLY this format (replace placeholders with real values):
             <atlas_no (if any)> (<atlas_name>) <page number>

        2. IF the title block is present but you can only find SOME fields (e.g. no atlas name), fill what you can and omit missing parts:
           - Only atlas number found: <atlas_no>
           - Only atlas name found: <atlas_name>
           - Use your best judgment for partial matches

        3. IF NO title block is found at all: summarize the most important content on this page in no more than 10 Chinese or English words.

        4. IF the page is completely blank or contains only meaningless noise: return exactly: null

        Requirements:
        - Output a SINGLE LINE only, no explanations, no prefixes, no extra text
        - Use the SAME LANGUAGE as the text visible on the page
        - Do NOT wrap the output in quotes or markdown
        - Do NOT add any explanation before or after the formatted string
        """

    # ==================== Table Processing Prompts ====================

    elif task == "detect-table-headers":
        temperature = 0.1
        context = f"        {texts}"

        prompt = f"""
        You are an intelligent assistant familiar with table data structures. You will receive the first few rows of a table (in HTML format).
        
        {context}
        
        Your task is: identify the header rows of this table, considering the possibility of MultiIndex (multi-level index) headers.
        You must strictly follow these requirements:
        - You need to determine the **consecutive rows that may constitute the header**, i.e., all rows of the MultiIndex
        - The result should be a **list of row numbers where headers are located** (0-indexed), for example:
            - If only the first row is a header, the result is `[0]`
            - If rows 1-3 are all headers (multi-level index), the result is `[0, 1, 2]`
        
        - If you cannot determine, please return an empty list `[]`
        - Only return a JSON object in the following format with key "answer" and value being the result:
        ```json
        {{
          "answer": [<row_number1>, <row_number2>, ...]
        }}
        """

    # ==================== TOC Detection Prompts ====================

    elif task == "detect-toc-range":
        temperature = 0.1
        max_tokens = 100
        start_idx = kwargs["paras"]["start_idx"]
        end_idx = kwargs["paras"]["end_idx"]

        prompt = f"""You are a document analysis expert. You need to identify the actual start and end positions of the table of contents from the candidate region.

        [Candidate Region Content]
        The following table shows candidate lines with their id (0-indexed) and content:
        {texts}

        [Judgment Rules]
        Please analyze the above content and find the actual start and end line ids of the table of contents region:

        1. **TOC Line Characteristics**:
        - Starts with "Table of Contents", "Contents", "目录", "目次", etc.
        - Usually contains chapter numbers or serial numbers (e.g., "1.", "Chapter 1", "一、", "第一章", etc.)
        - Contains heading text, usually with page numbers at the end
        - Format is relatively uniform and neat, with ellipsis "..." possible in the middle of line text
        - TOC region should include the line containing keywords like "Table of Contents", "Contents", "目录" if they exist

        [Output Format]
        Output in JSON format:
        {{
            "toc_start": number,  // TOC start id (must be in range {start_idx} to {end_idx})
            "toc_end": number,    // TOC end id (must be in range {start_idx} to {end_idx})
            "confidence": "high" | "medium" | "low"  // Confidence level
        }}

        If no TOC region matching the above characteristics exists in the input content, output:
        {{
            "toc_start": null,
            "toc_end": null,
            "confidence": "low"
        }}

        Output JSON only, do not output anything else
        """

    # ==================== Hierarchical Summary Prompts ====================

    elif task == "file-summary":
        max_tokens = kwargs["paras"].get("max_tokens", 100)
        node_name = kwargs["paras"].get("node_name", "")
        lang = kwargs["paras"].get("lang")
        has_self_only = bool(kwargs["paras"].get("has_self_only"))
        child_titles = kwargs["paras"].get("child_titles") or []
        self_only_content = kwargs["paras"].get("self_only_content", "(none)")
        covered_nodes = kwargs["paras"].get("covered_nodes", "(none)")
        if isinstance(child_titles, list):
            children_repr = ", ".join(str(t) for t in child_titles) if child_titles else "(none)"
        else:
            children_repr = str(child_titles) or "(none)"
        lang_directive = _language_directive(lang)
        lang_rule = (
            f"- **LANGUAGE (HARD CONSTRAINT)**: {lang_directive}"
            if lang_directive
            else "- Your response must be in the SAME LANGUAGE as the input text"
        )

        prompt = f"""SCOPE_TITLE: {node_name}
        SCOPE_STRUCTURE:
        - self_only: {"yes" if has_self_only else "no"}
        - children: [{children_repr}]
        
        SELF_ONLY_CONTENT:
        {self_only_content}

        COVERED_NODES:
        {covered_nodes}

        Your task:
        {lang_rule}
        - Produce ONE concise top-level summary of THIS scope (self_only content plus covered nodes), no more than {max_tokens} characters
        - Output the summary DIRECTLY, no prefixes, no explanations
        - If the input lacks meaningful text, return exactly: null
        """

    # ==================== Unknown Task ====================

    else:
        from loguru import logger
        logger.warning(f"Unknown task: {task}, returning empty prompt")
        prompt = ""

    return prompt, temperature, top_p, max_tokens
