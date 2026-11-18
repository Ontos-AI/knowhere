"""Prompts for the slide-structure route. Every image labels each slide with its number."""

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

__all__ = ["GAP_PROMPT", "STRUCTURE_PROMPT"]
