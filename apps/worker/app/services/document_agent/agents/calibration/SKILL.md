# Calibration SubAgent Skill

## Goal

For the **current TOC region**, discover page-numbering **regimes** and an
**initial offset** for each regime that has usable entries. Submit candidate
offsets via `calibration.submit`. After submit, a deterministic completion pass
runs the production tail-verify → binary-search → small-step recalibrate loop
(using the same visual page confirmer as production). Only **complete segments**
are usable for coarse structure; unrecognized pages are treated as **no TOC**.

## Do not use

- Do not scan a fixed window after the TOC (the old “TOC end + N pages” probe).
- Do not invent physical pages you did not inspect or obtain from `link`.

## Mandatory first step — partition regimes

Inspect every `page_number` label on the current TOC entries and partition them
into **page-numbering regimes** (distinct numbering systems / label shapes:
decimal digits, roman numerals, prefixed folio labels, etc.).

- Do not mix samples across regimes when computing an offset.
- Include `entry_indices` (0-based indices into `toc_region.entries`) for each
  regime you submit.
- Run the same initial-calibration procedure independently for each regime that
  has usable entries.

## Phase 1 — Initial offset (your job via tools)

For each regime:

1. Select a small set of entries (prefer spread: early / middle / late when
   enough entries exist).
2. Candidate physical page:
   - If the entry has `link.physical_page`, use it as the primary candidate.
   - Otherwise derive a coarse physical candidate from the printed label and
     `page_count`, then confirm with vision.
3. Call `inspect.pages` to confirm the heading starts on that page and to read
   the folio/printed label when useful.
4. If wrong, inspect nearby physical pages and revise.
5. Compute `offset = physical - printed` using this regime’s interpretation of
   the printed label.
6. Submit **candidate** offsets. Do not treat Phase 1 alone as a finished
   coarse-structure calibration.

## Phase 2 — Completion (deterministic after submit; production path)

For each TOC region with a candidate primary offset (prefer decimal):

1. Build TitleNodes via production `extract_toc_nodes` (integer `page_number`
   only; roman / prefixed labels become `printed_page=None`).
2. Run production `anchor_hierarchy_from_offset`:
   prune → tail verify → binary-search breakpoint → small-step recalibrate →
   null-page parent locate.
3. Emit production `SkeletonAnchor` (`offset`, `offset_status`,
   `match_overrides`, `null_page_report`, `bulk_count`, `pruned_count`,
   `locate_agent`).
4. On recalibrate/budget failure: keep the complete **prefix**; mark only the
   unresolved **suffix** as no TOC. Never fall back to a fixed post-TOC window.

## Usability bar

- Coarse structure may use the result when `SkeletonAnchor.offset_status=ok`
  and `bulk_count > 0` (at least one complete production segment).
- Otherwise downstream treats the document as no-TOC / Root fallback.

## Tools

- `inspect.pages`: primary tool for Phase 1. Open physical pages, render, answer
  your question. Prefer batching related pages when the same question applies.
- `calibration.submit`: finish Phase 1. Pass the full result under
  `tool_args.result` (or result fields directly in `tool_args`).

## Output rules

- Submit `status`, `regimes`, top-level `offset` / `offset_status` for the
  primary decimal-digit regime when identifiable, `tool_calls`, `notes`.
- Each regime must include `kind`, candidate `offset`, `offset_status`,
  `entry_indices`, `samples` (with `title`, `printed_label`, `physical` when
  known), and `posterior` if you already inspected a late check.
- Keep `kind` values consistent within one run (`decimal`, `roman`, `prefixed`,
  or `other`).
- Stay within the tool/round budget announced in the payload.
