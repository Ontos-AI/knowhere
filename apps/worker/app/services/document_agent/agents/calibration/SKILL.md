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
- Do not track a total page-count budget. Limits are **token budgets** and
  **max_rounds** in the payload. A per-call page cap is only a batch-size limit.

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
3. Progressive `inspect.pages` for that title (start small, expand only if needed):
   - **1st call**: inspect **1** candidate page only.
   - **2nd call** (if miss): inspect up to **3** nearby pages.
   - **3rd call** (if still miss): inspect up to **5** nearby pages.
   Never open with a full 5-page batch when a single page has not been tried.
4. Compute `offset = physical - printed` using this regime’s interpretation of
   the printed label.
5. Submit **candidate** offsets. Do not treat Phase 1 alone as a finished
   coarse-structure calibration.

If `inspect.pages` returns budget exhausted, or rounds run out before a reliable
offset: treat that sample / regime as **not found**, submit whatever regimes you
already confirmed (or `status=failed`), and let production fallback handle the
rest. Do not guess pages.

## Phase 2 — Completion (deterministic after submit; production path)

For each TOC region, every regime with a candidate offset is completed
independently, then merged by **physical page**:

1. Build TitleNodes via production `extract_toc_nodes` (regime-aware parse:
   decimal / roman / prefixed labels → `printed_page` + `page_kind`).
2. For **each** regime with an offset:
   - Project leaves belonging to that regime
   - Run production Phase-2: prune → tail verify → binary-search →
     small-step recalibrate (single-leaf regimes apply offset directly)
3. Merge all regime `match_overrides` (physical pages), then null-page parent
   locate once on the combined tree.
4. Emit production `SkeletonAnchor` (`offset` = primary decimal summary,
   `match_overrides` = union of all regimes, `null_page_report`, `bulk_count`,
   `pruned_count`, `locate_agent`).
5. On recalibrate/budget failure inside one regime: keep that regime's complete
   **prefix**; **drop** unresolved **suffix** leaves from the TOC tree (no TOC),
   then run null-page parent locate on what remains. Never fall back to a fixed
   post-TOC window.

## Usability bar

- Coarse structure may use the result when `SkeletonAnchor.offset_status=ok`
  and `bulk_count > 0` (at least one complete production segment).
- Otherwise downstream treats the document as no-TOC / Root fallback.

## Tools

- `inspect.pages`: primary tool for Phase 1. Open physical pages, render, answer
  your question. Prefer the progressive 1→3→5 schedule above. Per-call page
  count is capped; overall spend is limited by the calibration visual token
  budget and `max_rounds`.
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
- Stay within the token / round budgets announced in the payload.
