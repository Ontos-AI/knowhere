# PAGE body text previews

New PAGE chunks include `metadata.body_text_preview`, a string of at most 180
Unicode characters with whitespace collapsed. It comes only from PROFILE lines
explicitly classified as `region=body`. Headers, footers, summaries and full
chunk content are not fallback sources. A known empty body or unavailable
classification produces an explicit empty string.

The full text cache and PAGE `content`, chunk identifiers, page assets and
references remain unchanged. A separate in-memory body cache survives the
PROFILE-to-Page-Memory handoff and is excluded from serialized profile metadata.
Node assembly uses only pages physically owned by that chunk; shared `SAME-AS`
pages do not supply another node's preview. On section start pages, body text is
bounded by the actual current and following section headings. Ambiguous heading
boundaries produce no preview rather than borrowing adjacent section content.
Whole-document PAGE chunks follow the same body-only and length rules.

The field survives parser DataFrame conversion, normal and shared-demo
publication, HTTP chunk metadata, result ZIP projection and canonical demo
bundle storage. Existing results without the field remain valid. Readers must
use a page-number label when the preview is missing or blank; they must not
guess a preview from legacy full text.

## Verification

Validation ran in an isolated Linux ARM64 environment:

- All 409 Worker contracts pass, including PROFILE region preservation,
  unclassified legacy text, shared-page ownership, same-page section limits,
  explicit empty previews and whole-document length bounds.
- Three HTTP/PostgreSQL/storage contracts confirm normal-user and shared-demo
  metadata persistence, empty-string preservation and canonical ZIP contents.
- Ruff and Pyright pass for the changed implementation modules.

A replay of the actual SpaceX PDF physical page 20, using its saved profile
boundaries and the real node assembler, produced:

> GLOSSARY OF TERMS The terms and abbreviations defined in this section are used throughout this prospectus: • “AI” or “artificial intelligence” refers to advanced computational tech

This is a 180-character body preview for
`spacex-s1.pdf/GLOSSARY OF TERMS`. The original content remains intact, including
the page's existing margin text. This replay does not modify a published result.

## Historical results and optional demo backfill

This change affects new parses. It does not automatically repair old normal-user
results, cached Blob snapshots or immutable ZIPs. Those readers continue to use
page-number labels until trusted preview metadata is available.

A separate demo-only metadata backfill can avoid models and full document
parsing when the exact revision's classified line cache is retained. Otherwise,
the saved `header_y`/`footer_y` profile boundaries and the same normalized
`source.pdf` can reproduce the deterministic line classification. For DOCX,
use the retained normalized PDF; converting the original again may change page
layout. Missing profile, source or ownership evidence must be reported as a
skip, without inventing a preview.

Prepare a dry-run plan keyed by document revision and chunk identifier. Include
the content hash, previous field presence/value and proposed preview. Applying
a reviewed plan must recheck the current published revision and content hash,
update only `body_text_preview` in demo chunk metadata, and retain a reversible
journal. Repeated application must be idempotent; rollback must compare the
applied value before restoring the old field. Original content, references,
page assets and ZIP objects remain unchanged. Such a DB-only backfill benefits
HTTP demo reads and does not claim to update historical ZIP or Blob artifacts.
