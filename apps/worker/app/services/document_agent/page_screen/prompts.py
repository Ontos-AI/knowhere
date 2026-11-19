"""Prompt for the page-image screen. Every image labels each page with its number."""

SCREEN_PROMPT = """You are looking at a contact sheet: thumbnails of consecutive pages from ONE document (for example a report, a manual, a book or a slide deck), in reading order. Each cell is labeled with its page number in a red tag at its top-left corner.

Find the structural pages of the document:
- toc_pages: pages that list the document's sections, i.e. a table of contents or an agenda (including an agenda repeated between chapters, and a list of the sections of one part or chapter).
- divider_pages: pages that open a chapter or sub-chapter, i.e. section dividers.
A page that opens a section and lists that section's sub-sections belongs to both lists.
Judge every page on its own: another structural page on the same sheet (for example the document's main table of contents) does not make a page less structural.
Ordinary content pages, the cover, and thanks/Q&A pages are not structural.

Return strict JSON only:
{"toc_pages": [<page number>, ...], "divider_pages": [<page number>, ...]}
Page numbers must be the printed red tags. Return empty lists when no page is structural."""

__all__ = ["SCREEN_PROMPT"]
