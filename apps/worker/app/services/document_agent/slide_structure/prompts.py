"""Prompts for the slide-structure route. Every image labels each slide with its number."""

SCREEN_PROMPT = """You are looking at a contact sheet: thumbnails of consecutive slides from ONE presentation, in reading order. Each cell is labeled with its slide number in a red tag at its top-left corner.

For each slide decide yes/no: is it a structural slide of the deck, i.e. a table-of-contents / agenda slide (including agenda slides repeated between chapters) or a slide that opens a chapter or sub-chapter (a section divider)?
A slide that opens a section with its numbered heading and lists that section's sub-sections is structural too.
Judge every slide on its own: another structural slide on the same sheet (for example the deck's main table of contents) does not make a slide less structural.
Ordinary content slides, the cover, and thanks/Q&A slides are not structural.

Return strict JSON only:
{"structural_slides": [<slide number>, ...]}
Slide numbers must be the printed red tags. Return an empty list when no slide is structural."""

STRUCTURE_PROMPT = """You are given the candidate structural slides (table-of-contents / agenda slides and section dividers) of ONE presentation with {page_count} slides, each labeled with its slide number in a red tag. Some candidates may have been picked by mistake.

Work out the deck's section structure from these slides: which sections the deck has, the slide where each section starts, and how they nest. Use titles exactly as printed. Ignore candidates that are not structural.

For reference, common deck conventions: an agenda is often repeated before each chapter with the current chapter emphasized (fill, color, weight, box, or the others greyed out), and that repeat is where the chapter starts; dividers often carry a chapter number (01 / Part 2 / Chapter 2 / 3.2); a slide with a numbered heading that lists its sub-sections opens that section.

A section's start_slide must be a candidate slide that opens that section, so each candidate opens at most one section. A section that is only listed on a slide (an agenda, or a section slide listing its sub-sections) is not located there; report it as a gap instead.

Then infer gaps: sections that the evidence says should exist (numbering sequences, agenda items, chapter order) but that no candidate locates. Only report gaps at levels already present in your sections. For each gap give the candidate slides it must lie between (after_slide; before_slide, or null for the end of the deck).

Return strict JSON only:
{{"sections": [{{"title": "...", "start_slide": <int>, "level": <int>}}], "ignored_slides": [<int>],
  "gaps": [{{"expected": [{{"title": "<section>", "level": <int>}}], "after_slide": <int>, "before_slide": <int or null>}}]}}"""

GAP_PROMPT = """These slides (each labeled with its slide number in a red tag) come from one presentation. The following sections are expected to start somewhere among them:
{expected}

For each expected section give the slide where it starts, or null if none of these slides starts it. A slide that starts a differently numbered or differently named section does not count. Do not guess: null is better than a wrong slide. Use the title as printed on the slide when found.

Return strict JSON only:
{{"found": [{{"expected": "...", "title": "...", "start_slide": <int or null>}}]}}"""

__all__ = ["GAP_PROMPT", "SCREEN_PROMPT", "STRUCTURE_PROMPT"]
