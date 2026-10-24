# VLM Usage Points

Inventory of live places that send **page or image pixels** to a vision model.
Text-only LLM calls are listed at the bottom so they are not mistaken for VLM.

Checked against the worker parse path and retrieval attach path. Re-trace before treating a new call as compliant.

## Config

| Knob | Role |
|---|---|
| `IMAGE_MODEL` | One vision model for parse. Official id: `deepseek-flash` (DeepSeek-V4.1-Flash, native multimodal). Routes through `DS_KEY` / `DS_URL`. |
| `ASSET_MODEL` | Optional override for page-memory chart/table bbox only. Empty follows `IMAGE_MODEL`. Job metadata `page_memory_config.asset_model` still wins when set. |
| Request `llm_config.vision` | Per-request override at call time. Not a server env switch. |

`IMAGE_MODEL_MAX` is gone. Staging worker env may still pin an older name; local worker `.env` is what a local parse reads. Restart the worker after changing the env.

Already-created jobs that baked a concrete `asset_model` keep that pin.

## Shared PROFILE (PDF / PPTX, both parse tracks)

Runs inside document profile before page-memory or chunk parse.

| # | What it does | When it fires | Model |
|---|---|---|---|
| 1 | Coarse classify (scan / atlas / slides / generic) | Sample up to 10 pages | `IMAGE_MODEL` |
| 2 | Confirm TOC start pages | Text scan found candidates | `IMAGE_MODEL` |
| 3 | Extract TOC entries | After confirm | `IMAGE_MODEL` |
| 4 | Calibrate: walk forward to find where a TOC title first starts | Printed TOC beat the outline; used to compute printed→physical offset | `IMAGE_MODEL` |
| 5 | Calibrate: confirm the guessed page (usually one page) | Offset already computed; yes/no. If no, bisect then run 4 again | `IMAGE_MODEL` |
| 6 | Null-page locate: grep first, then confirm the start page | TOC entry has no printed page | `IMAGE_MODEL` |

If the PDF outline wins coverage, 4–6 are skipped.

### PROFILE 4–6 in one example

Cover + TOC occupy physical pages 1–3. Printed TOC says `1. Overview … 1`, `2. Method … 5`, `Appendix A` (no page).

- **4** asks “Does `1. Overview` start on this page?” from after the TOC. Finds physical page 4 → offset +3.
- **5** asks “Does `2. Method` start on page 8?” If yes, apply +3 to the rest. If no, find where the offset breaks.
- **6** searches text for `Appendix A`, then asks which of those pages is the real section start. A text hit is not enough.

## Page-memory track (API v2 `.pdf` / `.pptx`)

| # | What it does | When it fires | Model |
|---|---|---|---|
| 7 | Detect outline titles on fat leaves | Default: leaf ≥ 4 pages | `IMAGE_MODEL` |
| 8 | Per-page summary / entities | Almost every in-scope page; skip-tagged pages do not call | `IMAGE_MODEL` |
| 9 | Node summary | Section spans pages, or one page hosts several nodes. Single exclusive page reuses 8 | `IMAGE_MODEL` |
| 10 | Box figures/tables | Default on | `ASSET_MODEL` or `IMAGE_MODEL` |
| 11 | Summarize cropped assets | Default **off** | same as 10 when enabled |

## Text / chunk track (API v1, and non PDF/PPTX)

| # | What it does | When it fires | Model |
|---|---|---|---|
| 12 | Atlas page info | Coarse routing is atlas **and** chunk track. Page-memory ignores atlas routing | `IMAGE_MODEL` |
| 13 | Embedded image description | DOCX / Markdown (including PDF→Markdown) when image summary is on (default on) | `IMAGE_MODEL` |
| 14 | Standalone image upload | Classify type; text-like images also transcribe | `IMAGE_MODEL` |

Table / body summaries on this track are text LLM, not VLM.

## Retrieval (after parse)

| # | What it does | When it fires | Model |
|---|---|---|---|
| 15 | Attach HTTPS image URLs to the agent | Agentic retrieval on, and a tool returned a public image | Retrieval harness model, **not** `IMAGE_MODEL`. Cursor attaches HTTPS images. Own harness only attaches when the model name contains `vision`. |

## Not VLM

- Scanned-page text: local RapidOCR
- TOC source choose, cross-page table continuity, title-tree refine, Excel/DOCX table summary: text LLM
- Archived map-nav image filter: not on the live path

## Debug

Staged page-memory / text-track scripts reuse the same production calls. They are not extra VLM products.
