"""
Prompt Service for Document Parsing
Only contains prompts required for document parsing workflow.

Removed prompts for:
- RAG/Knowledge Base: talk-kb, merge-answers, judge-kb, rerank, connect-kb, detect-contradict
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
# Rationale: LLMs such as deepseek-chat have a strong prior toward Chinese when
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

    if task == "summary":
        max_tokens = kwargs["paras"]["max_tokens"]
        lang = kwargs["paras"].get("lang")
        lang_directive = _language_directive(lang)
        lang_rule = (
            f"- **LANGUAGE (HARD CONSTRAINT)**: {lang_directive}"
            if lang_directive
            else "- Your response must be in the SAME LANGUAGE as the input text"
        )
        prompt = f"""
        You will receive a text passage (which may include HTML tables or structured data):
        '''
        {texts}
        '''
        Your task and requirements:
        {lang_rule}
        - Extract the main content of the material, not exceeding {max_tokens} characters
        - If the input is an HTML table, summarize its structure and key data points in natural language, do NOT return the HTML code itself
        - If the input content is too short, mostly empty, or lacks meaningful text to summarize, return exactly: null
        - Output the summary content DIRECTLY, do not start with phrases like "Here is the summary"
        - Do not add any format wrappers, prefixes, or explanations beyond the summary
        """

    elif task == "summary-titled":
        max_tokens = kwargs["paras"]["max_tokens"]
        lang = kwargs["paras"].get("lang")
        lang_directive = _language_directive(lang)
        lang_rule = (
            f"- **LANGUAGE (HARD CONSTRAINT)**: {lang_directive}"
            if lang_directive
            else "- Your response must be in the SAME LANGUAGE as the input text"
        )
        prompt = f"""
        You will receive a text passage (which may include HTML tables or structured data):
        '''
        {texts}
        '''
        Your task:
        {lang_rule}
        - Line 1: Output a short title (no more than 15 characters) that captures the core topic
        - Line 2 onward: Output a detailed summary, not exceeding {max_tokens} characters
        - If the input is an HTML table, summarize its structure and key data points in natural language, do NOT return the HTML code itself
        - If the input content is too short, mostly empty, or lacks meaningful text, return exactly: null
        - Output DIRECTLY without any prefixes like "Title:" or "Summary:"
        """

    elif task == "summary-keywords":
        max_tokens = kwargs["paras"]["max_tokens"]
        kw_num = kwargs["paras"]["kw_num"]
        lang = kwargs["paras"].get("lang")
        lang_directive = _language_directive(lang)
        lang_rule = (
            f"- **LANGUAGE (HARD CONSTRAINT)**: {lang_directive}"
            if lang_directive
            else "- Keywords must be in the SAME LANGUAGE as the input text"
        )

        example = """
         {"answer":"<keyword_1>;<keyword_2>;<keyword_3>"}
        """

        prompt = f"""
        You will receive a text passage:
        '''
        {texts}
        '''
        Your task is to extract keywords, no more than [{kw_num}] keywords. Note:
        {lang_rule}
        - Your response must be in JSON dictionary format with key "answer" and value being the extracted keywords
        - Keywords should reflect the text theme, separated by semicolons ";"
        - Example format:
        {example}
        - Do not output any additional explanations or descriptions besides the keywords
        """

    elif task == "summary-full":
        max_tokens = kwargs["paras"]["max_tokens"]
        kw_num = kwargs["paras"].get("kw_num", 3)
        lang = kwargs["paras"].get("lang")
        lang_directive = _language_directive(lang)
        if lang_directive:
            lang_line = (
                f"**LANGUAGE (HARD CONSTRAINT, applies to EVERY field — title, "
                f"keywords, and summary)**: {lang_directive}"
            )
        else:
            lang_line = (
                "**First and most important**, all output must be in the "
                "**SAME LANGUAGE** as the input text"
            )

        example = """
         {"title":"<title>","keywords":"<keyword_1>;<keyword_2>;<keyword_3>","summary":"<summary>"}
        """

        prompt = f"""
        You will receive a text passage (which may include HTML tables or structured data):
        '''
        {texts}
        '''
        Your task is to extract a title, keywords, and summary from this content.
        {lang_line}

        Other requirements:
        - Your response must be in JSON format with exactly three keys: "title", "keywords", "summary"
        - "title": a short title capturing the core topic, no more than 15 characters
        - "keywords": the most important thematic keywords, no more than {kw_num}, separated by semicolons ";"
        - "summary": a concise summary of the main content, not exceeding {max_tokens} characters
        - If the input is an HTML table, summarize its structure and key data points in natural language
        
        - If the input content is too short, mostly empty, or lacks meaningful text, return exactly: null
        - Example format:
        {example}
        - Do not output any additional explanations or descriptions
        """

    # ==================== Heading/Structure Prompts ====================

    # ---------------------------------------------------------------------
    # LEGACY `eval-headings` prompt — designed for FULL-TEXT input, before
    # `_compact_for_llm` collapses consecutive body rows into placeholders.
    # Kept as reference; DO NOT delete.  The live prompt below targets the
    # COMPACT input shape used when `KB_LAYOUT_LLM_COMPACT_INPUT` is on
    # (default). The live prompt below is the publication baseline.
    # ---------------------------------------------------------------------
    #     elif task == 'eval-headings':
    #         temperature = 0
    #         top_p = 0.01
    #         max_depth = kwargs['paras']['max_depth']
    #         max_tokens = kwargs['paras']['max_tokens']
    #         toc_context = kwargs['paras'].get('toc_context', '')
    #
    #         # developing toc context (if any)
    #         if toc_context:
    #             toc_section = f"""
    #         ***Important Reference: Table of Contents (TOC)***
    #         The following is the document's table of contents with predefined levels. Use this as a reference when assigning levels:
    #
    #         '''
    #         {toc_context}
    #         '''
    #
    #         - If a row's heading matches a TOC entry, use the TOC's predefined level
    #         - If a row appears to be a sub-section of a TOC entry, assign a deeper level
    #         - IMPORTANT: If a row does NOT appear in the TOC, it CAN ONLY be set as either a body text (level = -1) or sub-section with a deeper level than the nearest TOC heading above it
    #         """
    #         else:
    #             toc_section = ""
    #
    #         prompt = f"""
    #         You are a document structure auditing expert. You will receive a Markdown table with text rows, where each row may be a heading or body text, including:
    #         1. id column: line number
    #         2. heading column: text content
    #         3. level column: preliminary estimated level (may be inaccurate or missing), where:
    #             1 represents `<h1>` (highest), 2 represents `<h2>`, and so on
    #             -1 indicates the text is estimated as body text (not a heading)
    #             "Not Sure" indicates the level is undetermined
    #
    #         Data to be adjusted:
    #         '''
    #         {texts}
    #         '''
    #
    #         {toc_section}
    #
    #         ***Placeholder Rows***
    #         Some rows may appear as "[N BODY LINES]" with an id like "55-63" (a range) or
    #         "56" (a single line), and level column rendered as "-".  These are NOT real
    #         candidates — they are compact markers representing N consecutive body-text
    #         lines that have been collapsed to save space.  Treat them only as positional
    #         context (they tell you how many body lines sit between two adjacent heading
    #         candidates).
    #         - You MUST NOT emit placeholder rows in your output.
    #         - The output id field MUST be a single integer; never return an id containing
    #           a hyphen ("-") or the level placeholder "-".
    #         - Only evaluate rows whose id is a single integer.
    #
    #         ***Process in THREE steps:***
    #
    #         **STEP 1 — Global Pattern Scan (before assigning any levels)**
    #         Scan ALL candidate heading rows across the entire input.
    #         Identify every distinct structural/numbering pattern that signals hierarchy depth, for example:
    #         - Decimal numbering: "1", "1.1", "1.1.1" → depth increases with dot count
    #         - Enumeration styles such as Chinese numerals, numbered bullets,
    #           or circled digits map from shallower to deeper levels
    #         - Chapter/section keywords: "Chapter X", "Part X", and Chinese
    #           chapter/section markers
    #         - Indentation or formatting cues visible in the text prefix
    #         Rank these patterns from shallowest to deepest to form a pattern → level mapping.
    #
    #         **STEP 2 — Assign levels using the following rules (in priority order)**
    #         Rows marked as "Not Sure" should be treated like any other candidate row:
    #         use the same rules below to decide whether they are true headings (level >= 1)
    #         or body text (level = -1).
    #
    #         Rule 0 — Figure/Image rows are always body text (highest priority, no exceptions):
    #             Any row whose heading text is exactly "Figure/Image" MUST be assigned level = -1.
    #             These represent embedded images, figures, or inline resource references in the document.
    #             Do NOT include these rows in the output (they are automatically treated as level = -1).
    #         Rule 1 — Normalize to start at level 1:
    #             The shallowest heading pattern found in this document MUST be assigned level 1.
    #             Do NOT preserve preliminary estimates that start at level 2, 3, or deeper
    #             if those headings are actually the top-level headings of the document.
    #         Rule 2 — Global consistency (highest priority among content rules):
    #             Headings that share the same structural pattern SHOULD receive the SAME level
    #             throughout the ENTIRE document, regardless of their position or textual content.
    #             (e.g., all "X.Y" two-part numbers must have the same level; all "X.Y.Z"
    #             three-part numbers must share a different, deeper level.)
    #         Rule 3 — Pattern over semantics:
    #             When determining a heading's level, its numbering/structural pattern takes
    #             precedence over its text length or semantic meaning.
    #             Parenthetical annotations or long descriptions inside a heading text do NOT
    #             indicate a different hierarchy level.
    #         Rule 4 — Parent-child continuity and no level skipping:
    #             Each heading must be consistent with adjacent headings.
    #             A heading may stay at the same level, return to an ancestor level,
    #             or go only ONE level deeper than its nearest valid ancestor heading.
    #             Level jumps such as level 1 directly to level 3 are invalid.
    #         Rule 5 — Body text demotion:
    #             If a row does not truly serve as a section title in the document outline,
    #             set its level to -1.
    #             Strong body-text cues include:
    #             - a full sentence or clause ending with sentence punctuation
    #             - an isolated broken word, broken phrase, label fragment, data value, or body continuation
    #             - a single Chinese character, digit, or very short fragment that clearly combines
    #               with the next row to form one continuous phrase rather than a standalone heading
    #         Rule 6 — Semantic heading promotion:
    #             A row with NO obvious numbering or structural-format markers can still
    #             be a heading, but ONLY when ALL of the following conditions are met:
    #             (a) The text is short and title-like (not a full sentence with punctuation).
    #             (b) It is NOT a broken fragment that simply continues into the next row
    #                 (those belong to Rule 5 body-text demotion).
    #             (c) Multiple longer body-text rows follow it, and the row clearly
    #                 organizes, summarizes, or introduces the topic of those rows —
    #                 i.e., removing it would leave the following rows without a
    #                 meaningful section label.
    #             Being short alone is NOT sufficient; the row must demonstrably serve
    #             as a section boundary that groups the content below it.
    #             When promoting, assign a level consistent with the surrounding
    #             hierarchy — typically one level deeper than the nearest heading above.
    #
    #         **STEP 3 — Consistency check (one pass) before writing output**
    #         Scan the level assignments you are about to output and confirm:
    #         - All headings sharing the same structural or semantic pattern have been assigned the same level.
    #         If any inconsistency is found, normalise to the most representative level for that pattern.
    #
    #         ***Output requirements***
    #         - Output must be a [JSON array] only
    #         - **Only include rows that you judge to be headings** (level >= 1). Do NOT include body text rows (level = -1) in the output
    #         - Any row not present in your output will be automatically treated as body text (level = -1)
    #         - Each element must contain the following fields in order:
    #             - "id": original line number (integer)
    #             - "level": the corrected heading level (integer from 1 to {max_depth})
    #
    #         ***Format requirements***
    #         - Output only valid JSON — do not add markdown fences (no ```json)
    #         - Do not add escaped newlines or other control characters
    #         - Do not add any explanations, comments, or descriptive text
    #         """

    elif task == "eval-headings":
        # COMPACT-input variant.  Input is pre-compressed by `_compact_for_llm`
        # so that consecutive body-text rows are folded into a single
        # ``[N BODY LINES]`` placeholder row.  The LLM therefore sees only:
        #   * heading CANDIDATES (integer id, real heading text), and
        #   * PLACEHOLDER rows (id with "-" or a single collapsed id, heading
        #     "[N BODY LINES]", level "-") that carry positional / section-bulk
        #     signals.
        temperature = 0
        top_p = 0.01
        max_depth = kwargs["paras"]["max_depth"]
        max_tokens = kwargs["paras"]["max_tokens"]
        toc_context = kwargs["paras"].get("toc_context", "")

        if toc_context:
            toc_section = f"""
        ***Important Reference: Table of Contents (TOC)***
        The following is the document's table of contents with predefined levels.
        Use it as a prior when assigning levels to CANDIDATE rows:

        '''
        {toc_context}
        '''

        - If a candidate's heading matches a TOC entry, use the TOC's predefined level.
        - If a candidate appears to be a sub-section of a TOC entry, assign a deeper level.
        - If a candidate does NOT appear in the TOC, it can ONLY be either body text
          (level = -1) or a sub-section deeper than the nearest TOC heading above it.
        """
        else:
            toc_section = ""

        prompt = f"""
        You are a document structure auditing expert. The input you receive is a
        COMPACT skeleton of a document.  Body-text lines have already been collapsed for 
        you so that every row is one of two kinds:

        1) HEADING CANDIDATE — ``id`` is an integer. ``heading`` is the candidate text.
           ``level`` is a preliminary estimate: a positive integer (1 = shallowest, deeper = larger)
           or the string "Not Sure" (undetermined). These rows — and ONLY these — are the ones you must evaluate.

        2) PLACEHOLDER — ``id`` is ALWAYS a hyphenated range "start-end" (for a
           single-line it is "N-N", e.g. "56-56"); ``heading`` is "[N BODY LINES]"
           where N is the number of body lines folded here; ``level`` is the literal "-".
           Placeholders are positional markers that tell you how many body lines sit
           between adjacent candidates. Use them as context ONLY.

        Data to be adjusted:
        '''
        {texts}
        '''

        {toc_section}

        ***Hard rules about placeholders***
        - Placeholders are NEVER candidates. Do not output them.
        - Every ``id`` in your output MUST be a single integer; never emit an id
          containing a hyphen ("-").  Never emit the level string "-".
        - Use N in ``[N BODY LINES]`` as a "section bulk" signal when applying
          the rules below (Rule 6 in particular).

        ***Process in TWO steps:***

        **STEP 1 — Global Pattern Scan (CANDIDATES ONLY)**
        Enumerate every distinct numbering / structural / semantic granularity pattern that
        appears on candidate rows and signals hierarchy depth, for example:
        - Decimal numbering: "1.", "1.1", "1.1.1" → depth increases with dot count
        - Enumeration styles: "一、" "（一）" "1、" "①" "1 " → shallower to deeper with increasing numbers
        - Chapter/section keywords: "Chapter X", "Part X", "第X章", "第X节"
        - Upper case / lower case differences in candidate headings
        - Clear semantic granularities or groups of themes
        Rank these patterns from shallowest to deepest to form a pattern → level mapping.
        Placeholder rows MUST NOT influence this scan.

        **STEP 2 — Assign a level to every candidate (rules in priority order)**
        A candidate whose preliminary ``level`` is "Not Sure" or any positive
        integer is **always** open to revision. Pure body text has already been
        folded into placeholders, but a candidate **can still be** demoted to level = -1.

        Rule 0 — Global consistency:
            Candidates sharing the same structural pattern or semantic granularity SHOULD receive the
            SAME level across the ENTIRE input. (e.g. every "X.Y" pattern
            shares one level; every "X.Y.Z" shares a different, deeper level.)

        Rule 1 — Parent-child continuity and no level skipping:
            A heading, compared to candidates before it, may stay at the same level, 
            or go ONE level deeper than its nearest valid ancestor heading.
            However, jumps such as level 1 → level 3 are **always invalid**.

        Rule 2 — Semantic headings detection (NO numbering pattern):
            A candidate WITHOUT any structural/numbering marker can still be a
            heading, but ONLY when ALL of the following hold:
            a) The text is short and title-like — no sentence-ending punctuation.
            b) It is NOT a broken fragment that continues into the next row
            c) In the input sequence it is IMMEDIATELY followed by a
                placeholder ``[N BODY LINES]``, or by another candidate with finer granularity.
                This is the "section bulk" signal — the row introduces a body block or a subsection group.
            When Rule-2 is satisfied, pick a level consistent with Rule 1

        Rule 3 — Body text demotion (candidate → -1):
            Demote a candidate to level = -1 when it clearly does NOT serve as a section title. 
            In compact input, the strongest demotion cues are:
            - Two CANDIDATE rows appear adjacent with NO placeholder between them
            - The text contains equations and math symbols such as + = - × ÷.
            - The text is exactly "Figure/Image", demote it to level = -1.
            - The text is an isolated broken phrase, fragment, data value, or caption-like snippet (e.g. "Table 3-2", "Figure 4"

        Rule 4 — Normalise to start at level 1:
            The shallowest (the most coarse granularity) heading found MUST be assigned level 1.

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

    # ==================== Image Processing Prompts ====================

    elif task == "summary-images":
        temperature = 0.1
        max_tokens = int(kwargs["paras"]["max_tokens"] * 1.2)
        if texts.strip():
            img_context = f"- Image context is [{texts}], you may reference the title for summarization"
        else:
            img_context = ""

        prompt = f"""
        You will receive an image, which may be a photo, chart, or an image requiring OCR.
        Your task is to extract the main content described in the image. Note:
        - Line 1: Output a short title (no more than 15 characters) summarizing the image's core topic
        - Line 2 onward: Provide a precise and concise summary, using text descriptions only, avoid extracting specific data from the image
        - Your response **MUST BE in the SAME LANGUAGE** as any text visible in the image (if there is no text, English is preferred)
        - If the image is blank, unreadable, or contains no meaningful content, return exactly: null

        {img_context}
        - Output DIRECTLY without any prefixes like "Title:" or "Summary:" or "This image shows"
        - Do not add any format wrappers, prefixes, or explanations beyond the content
        """

    elif task == "ocr-image":
        temperature = 0.1

        prompt = """
        You will receive an image, which may be a photo, chart, or an image requiring OCR.
        Your task is to perform OCR operation, fully extract and return the image content. Note:
        - **MUST Preserve the ORIGINAL LANGUAGE** of the text in the image
        - If the image contains no readable text, return exactly: null
        - Output the text content DIRECTLY, do not start with phrases like "The text reads"
        - Do not add any format wrappers, prefixes, or explanations beyond the text content
        """

    elif task == "ask-image":
        temperature = 0.1
        max_tokens = int(kwargs["paras"]["max_tokens"] * 1.2)

        prompt = f"""
        You will receive one or more images and the user's current question: [{query}]
        You may also receive context related to the image(s).
        
        {texts}
        
        Your task is to answer the user's question based on the image(s) and context (if any). Note:
        - Your answer must be in the SAME LANGUAGE as the user's question
        - Provide a complete and accurate answer with some explanation, but not exceeding {max_tokens} characters
        - If the image content is unrelated to the user's question, return exactly: null
        - Do not return any additional explanations or descriptions beyond the answer
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
        lang_directive = _language_directive(lang)
        lang_rule = (
            f"- **LANGUAGE (HARD CONSTRAINT)**: {lang_directive}"
            if lang_directive
            else "- Your response must be in the SAME LANGUAGE as the input text"
        )

        prompt = f"""You will receive summaries of sub-sections from a document section called "{node_name}":
        '''
        {texts}
        '''
        Your task:
        {lang_rule}
        - Produce ONE concise sentence summarizing ALL sub-sections, no more than {max_tokens} characters
        - Output the summary DIRECTLY, no prefixes, no explanations
        - If the input lacks meaningful text, return exactly: null
        """

    # ==================== Unknown Task ====================

    else:
        from loguru import logger

        logger.warning(f"Unknown task: {task}, returning empty prompt")
        prompt = ""

    return prompt, temperature, top_p, max_tokens
