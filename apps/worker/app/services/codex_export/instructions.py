"""Generate evidence-boundary instructions shipped with every review package."""

from __future__ import annotations


def build_codex_review_instructions(document_id: str) -> str:
    return f"""# Codex Review Instructions

Document ID: `{document_id}`

1. Read `structured/document_tree.json` for navigation only. The hierarchy is
   parser-assigned structure and is not source evidence.
2. Use `structured/blocks.jsonl` for extracted textual evidence. Distinguish a
   source derivative from parser inference, machine-generated visual
   description, limitation, and native verification required.
3. Use table HTML or CSV only with native page verification for
   decision-relevant values. CSV is a best-effort derivative.
4. Use `pages/page-*.png` to verify visual structure, signatures, tables,
   figures, and formatting against the native source.
5. Cite the document ID, page number, block ID, and table ID when available.
6. Do not cite generated summaries as source evidence.
7. Do not infer approval, status, disposition, compliance, equivalence, or
   Pass/Fail.
8. Flag extraction ambiguity and any missing native verification.
9. DOCX rendered pages, when present, come from a normalized LibreOffice PDF;
   they are not stable native DOCX page numbers and are not automatically
   mapped from MinerU logical pages.
"""
