# PDF TOC Phase 2: Keep Unnumbered TOCs

Status: proposal (2026-11-18). Scope: PDF TOC route only. The PPT route and
the page screen are unchanged.

## 1. Background and goal

### 1.1 Where this comes from

- The page screen is the thumbnail pre-scan shared by the PDF and PPT routes.
  On the PDF route, its `toc_pages` are unioned with keyword hits as TOC
  candidates. So the screen cost is already paid.
- The PDF TOC route was built for TOCs with printed page numbers. Their
  entries are placed by offset calibration and the null-page probe.
- Page numbers were required on purpose:
  - Locating every entry of an unnumbered TOC with the null-page probe is
    costly.
  - FINE refines the structure inside each scope anyway.
- As a result, the unnumbered TOCs that the screen finds are lost:
  - TOC confirmation requires titles paired with page numbers. It rejects
    section lists without page numbers, such as Sydney pages 104, 128, 166,
    202, 214, 226, 248 and 260.
  - The few unnumbered TOCs that pass, such as Sydney 327–328 and 432, still
    go through pairing, nesting and calibration. They fail calibration, and
    their nodes are dropped. On the way they cost VLM calls and can truncate
    another TOC's calibration scope.

### 1.2 Goal

Phase 2 only **keeps** the unnumbered TOCs that the screen finds. How to use
them for the coarse hierarchy is decided later.

- An unnumbered TOC is confirmed and extracted like any TOC.
- It does not enter nesting or calibration, so it costs nothing there and
  never disturbs the numbered TOCs.
- Its pages are claimed by a TOC node, so they are not body pages and FINE
  skips them, as for numbered TOCs today.
- Each distinct TOC owns its own TOC node. Only a paired copy of a TOC (for
  example the other language version) shares that TOC's node.

Phase 2 does not change the coarse rows: no row is cut, moved or added,
apart from TOC nodes. What it does not do, and why, is in §5.

## 2. Changes

### 2.1 TOC confirmation accepts lists without page numbers

In the flow this is one step: confirm whether a page is a TOC. In the code,
two VLM prompts both make this judgment, and both are relaxed. If only one
were relaxed, the page would still be dropped by the other.

- `_CONFIRM_PROMPT` (`tools/extract_toc_with_boundaries.py`)
  - A page that lists the document's sections counts as a TOC start, with or
    without page numbers.
  - The intro no longer claims every candidate had a TOC keyword. Candidates
    also come from the page screen.
- `VLM_TOC_BATCH_PROMPT` (`tools/vlm_toc_extractor.py`)
  - This prompt marks each page of the window as TOC or not, and the kept TOC
    pages come from it. Page numbers become optional there too.
  - It already returns `page_number: null` when no page reference is
    visible.

Today the outcome for unnumbered lists depends on the model's soft judgment.
327 and 432 pass on systematic numbering or a "Contents" heading. 104, 128
and the other chapter openers are rejected as "a divider that lists
sub-sections but has no page numbers". The relaxed prompts make this
consistent.

The rejection rules stay. Body text, a single-heading divider, a blank page,
a table or image page, and a glossary are still not TOCs. So Sydney 20, 21,
418, 422 and 426 stay rejected.

Risk: lists without page numbers also appear in body text, for example a
bullet list of objectives. A false positive would remove a body page from
anchoring and give it a TOC node. The prompt tests in §6 cover this.

### 2.2 Unnumbered TOCs skip pairing, nesting and calibration

- **Definition.** A TOC extracted from pages (`toc_range_unit == "page"`)
  where no `toc_with_level` row has a `page_number`.
  - This is read from the extracted entries. It is known before calibration
    and needs no extra VLM judgment.
  - A TOC with some numbered entries is a numbered TOC and keeps today's
    path.
- **One helper.** A single shared helper separates unnumbered TOCs from the
  hierarchies before `select_global_toc_hierarchies`. It is used at both
  call sites: `run_toc_anchoring` and `extract_section_skeletons`.
  - Unnumbered TOCs are never the primary TOC or a pending TOC. They get no
    pairing, nesting, calibration or graft.
  - They no longer end another pending TOC's calibration scope.
- **Why each step is skipped.**
  - **Calibration.** Without page numbers there is no offset, so calibration
    always fails.
  - **Pairing and nesting.** Their results (pair host, parent TOC and
    calibration scope) only feed calibration and the graft. An unnumbered TOC
    needs neither.
  - **Ending another TOC's scope.**
    - Today each pending TOC caps the calibration scope of the TOC before it.
      This comes from PR #200, which treats several TOCs as bound volumes, so
      that one part's TOC cannot anchor into the next part through repeated
      titles or restarted page numbers.
    - An unnumbered TOC in that list truncates the TOC before it for no gain.
      In Sydney, 432 truncated 361.
  - **Why not keep them in the nesting stack anyway.** An unnumbered TOC on
    the stack could become the parent or the pair host of a later numbered
    TOC. A numbered TOC nested under it would have no forest to graft into,
    and a numbered TOC paired to it would skip calibration and lose its
    structure. Avoiding both would need extra rules, and each unnumbered TOC
    would add 1–2 VLM nesting checks.
- **Accepted edge case.** Suppose a bound volume's later part has an
  unnumbered TOC.
  - The earlier part's calibration scope now extends into the later part.
  - Its numbered entries are not affected, because the offset maps them onto
    its own pages.
  - The offset search range and the null-page windows may match a heading
    with the same name in the later part.
  - This case is judged rare and limited.
- **Decided: no null-page probe over an unnumbered primary.**
  - Today the earliest TOC is the primary even when it is unnumbered. Its
    calibration fails, `anchor_hierarchy` falls back to
    `anchor_hierarchy_from_offset` with no offset, and the null-page probe
    runs on every entry. (An unnumbered pending TOC is dropped without a
    probe.)
  - Now the primary is chosen from numbered TOCs only. A document whose TOCs
    are all unnumbered gets the Root fallback (`no_toc`), which has no TOC
    node (§2.3).
  - The null-page probe itself is unchanged and still runs for unnumbered
    entries inside numbered TOCs.
- **The outline judge reads only numbered TOC pages.**
  - When the PDF has bookmarks, `judge.toc_source` compares the outline with
    the text of the printed TOC pages. This runs before the split.
  - Unnumbered TOC pages are left out of that text: when the judge picks the
    printed TOC, only numbered TOCs can be anchored. Otherwise a PDF with
    bookmarks and only unnumbered TOCs could lose its outline and fall back
    to Root.
  - With no numbered TOC pages, the outline wins without a judge call, as it
    does today with no TOC pages.
  - `toc_result.toc_pages` still holds every TOC page, so the outline route
    still excludes them from body pages.
- **Unchanged.**
  - Unnumbered TOCs stay in `blackboard.toc_hierarchies`, and their pages stay
    in `toc_result.toc_pages`.
  - The current TOC-page rules apply to them as to any TOC page: their pages
    are not body pages for anchoring, and only a TOC's last page may be a
    section start.
- **Side effect on numbered TOCs.**
  - Newly confirmed TOC pages leave the body pages used by the numbered TOCs'
    title search, calibration and null-page probe.
  - The main TOC's page numbers point to body pages, not to openers, so the
    effect is expected to be small.
  - The live comparison in §6 checks that existing starts do not change.

### 2.3 One TOC node per TOC (`_toc_ownership_skeletons`)

**Today.** `_toc_ownership_skeletons` groups the pages of all TOCs by parent
row and makes one `<parent>/TOC` node per parent. This came from the first
TOC-node change (2026-10-27), whose only aim was to claim TOC pages so FINE
skips them. Grouping by parent was the simplest way to do that, and it also
covered paired copies.

**Problem.** Two distinct TOCs under one parent are two different listings.
One merged node hides that. For example, two root-level TOCs on pages 10–11
and 14–15 become one TOC node over 10–15. With unnumbered TOCs kept, this
case becomes more frequent.

**Change.**
- Every TOC that is not a paired copy gets its own TOC node over its own TOC
  pages. This covers numbered and unnumbered TOCs alike.
- A paired copy's pages join its host's TOC node. Paired copies are the
  numbered TOCs that the nesting stack or the equal-span check marked.
- **Placement** is unchanged: the node goes under the open section of its
  pages (latest start, then deepest), or at document level when no section
  holds them.
- **Names**, per parent row:
  - one TOC node under the parent: `TOC`, as today;
  - several: each is `TOC p.<first page>`.
- Unchanged:
  - TOC nodes are made before the document prefix is inserted, so the prefix
    still ends before the first TOC node.
  - The Root fallback returns make no TOC node, as today.
  - Downstream, a TOC-node scope only claims TOC pages, so scopes, page claims
    and FINE need no change.

## 3. Sydney (from the live run, 2026-11-18)

Confirmed TOCs: 9 in total.

| TOC | Kind | Path |
|---|---|---|
| 5 | numbered (main) | primary |
| 284 | numbered (Appendix C) | nesting: inside 5; grafted under C |
| 361 | numbered (Appendix F) | nesting: not inside 284, inside 5; grafted under F |
| 128, 166, 202, 214, 226 | unnumbered (chapter openers) | kept, TOC node only |
| 432 | unnumbered (G2 opener) | kept, TOC node only |

Result:
- Each unnumbered TOC gets a TOC node under the open section of its page:
  `E3/TOC` (128), `E4/TOC` (166), `E5/TOC` (202), `E6/TOC` (214) and
  `E8/TOC` (226). The openers sit 1–2 pages before their chapter's coarse
  start. `G2/TOC` (432) existed before Phase 2.
- No parent holds two distinct TOCs, so every node is named `TOC`.
- All other rows are the same as before Phase 2: no range changed.
- 361 is a gain of the nesting stack. Under the old span check it was
  `parallel`, because its span ends after the main TOC's span, so its entries
  landed at document root. Now it is grafted under F.

Screen and confirmation vary between runs: in this run 104, 248, 260 and
327–328 were rejected by confirmation (327–328 passed in earlier runs, which
gave a `TOC` node under D Signage Code).

## 4. Cost

- No new kind of VLM call.
- Each newly confirmed TOC start runs the existing region extraction, which
  is one batch call over its window.
- Unnumbered TOCs no longer run pairing, nesting or calibration calls.
- One TOC node per TOC adds no calls.

## 5. Out of scope

- **Using unnumbered TOCs to shape the coarse hierarchy.** An earlier draft
  cut the row holding each TOC page. On Sydney, whose main TOC already lists
  every chapter, every cut only split off a 2–3 page tail before the next
  chapter. How to use these TOCs is decided later.
- **Locating the entries of unnumbered TOCs.** It needs a null-page probe per
  entry, which is costly. FINE refines inside each scope anyway.
- **Fixing chapter starts that sit after their opener page.** This is a 1–2
  page offset. An opener carries almost no body text, so the effect on
  retrieval is small.
- **A section level for a grafted sub-TOC.** A contained numbered TOC's
  entries are grafted under the primary's rows. If the primary does not list
  the chapter the sub-TOC belongs to, its entries sit directly under the
  covering row, without a node for that chapter. Adding one needs a change
  to the graft and is rare.
- **Divider pages** (the screen's `divider_pages`). Phase 2 uses only the
  screen's `toc_pages`.
- **The page screen and its prompt.** The prompt is shared with the PPT
  route, so changing it would change PPT.
- **The PPT route, including its gap filling.**
  - Gap filling looks at every page of a scope on contact sheets.
  - That is too costly for long PDFs.
- **Same-chapter duplicates on a boundary page across two TOCs.** This is a
  separate known issue.

## 6. Tests

- **Selection:** unnumbered TOCs never reach the primary or pending lists,
  are never nested or calibrated, and never end another pending TOC's scope.
- **Outline judge:** it reads only numbered TOC pages; with only unnumbered
  TOCs the outline wins without a judge call; `toc_result.toc_pages` keeps
  every TOC page.
- **TOC nodes:**
  - an unnumbered TOC gets a TOC node under the open section of its pages,
    and no other row changes;
  - two distinct TOCs under one parent give `TOC p.<a>` and `TOC p.<b>`;
  - a single TOC under a parent stays `TOC`;
  - a paired copy shares its host's node;
  - a document with numbered TOCs that each sit under their own parent gives
    the same rows as today.
  - This changes today's expected output where distinct TOCs share a parent,
    for example `test_parallel_root_tocs_merge_page_union`.
- **Prompts:** VLM answers are not covered by contract tests. The live run
  checks them: unnumbered section lists are kept, and Sydney 20, 21, 418, 422
  and 426 stay rejected.
- **Live:**
  - Run Sydney Stage 1–2 and compare TOC pages, skeleton rows, VLM calls and
    cost with the run before Phase 2.
  - Check that the coarse starts of the numbered TOCs do not change (§2.2,
    side effect).
