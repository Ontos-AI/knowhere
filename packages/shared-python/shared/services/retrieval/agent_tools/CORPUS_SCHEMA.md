# Knowhere Corpus Schema (agent-facing)

Single source of truth for how an agent should understand and explore a
Knowhere corpus through the tools in this package. This text is intended to
be shipped verbatim as both the API `/mcp` server instructions and the
`agent_explore` system prompt — do not duplicate it elsewhere; edit here.

This describes the **published, DB-served corpus** (`documents`,
`document_sections`, `document_chunks`, `graph_nodes`, `graph_edges`) that
the tools below are designed to query. It is not the on-disk parse artifact schema
(`chunks.json` / `doc_nav.json` on disk use different join keys and a
different path separator) — that schema is for the parser pipeline, not for
this tool set.

---

## 1. Corpus model

```text
Namespace
  └─ Document        (document_id, source_file_name, parse_track)
       └─ Section     (section_id, parent_section_id, section_path, section_level, summary)
            └─ Chunk  (chunk_id, chunk_type, content, chunk_metadata)
```

- **Namespace**: the retrieval scope. One namespace can hold documents parsed
  by different tracks (see §2) — do not assume a namespace is uniform.
- **Document**: `document_id` is the stable identifier for every other tool
  call. `parse_track` is `page_memory` or `chunk` (see §2).
- **Section**: the navigable tree. `section_path` segments are joined with
  `" / "` (space-slash-space), one segment per heading/synthetic level.
- **Chunk**: the retrieval unit. `chunk_type` is `text`, `page`, `image`, or
  `table`. A section owns **at most one body chunk** (`text` or `page`) —
  but which sections get one is track-dependent (see §2). `image`/`table`
  chunks are **not** attached to the section they are conceptually "in" —
  see §3.

Document-level relationships (`related` edges, including entity-overlap
scoring) exist in a separate graph — see §4.

## 2. Body chunks: `text` vs `page`, and the `SAME-AS` pointer

A namespace can mix both tracks across documents (e.g. one PDF on the
`page_memory` track next to one DOCX/XLSX on the `chunk` track). Treat
`text` and `page` as the same *role* — both are a section's own body — but
they differ in shape, and in **which sections get a body chunk at all**:

- **`chunk` track** (`text` chunks, e.g. DOCX/XLSX/MD): a section owns a
  body chunk if it has any content of its own directly under its heading,
  before any child heading — this can be **any section, leaf or not**. Do
  not assume a section with children has no body text of its own; check
  whether it owns a chunk instead of assuming from tree position.
- **`page_memory` track** (`page` chunks, PDF): only **leaf** sections own a
  body chunk. Internal/structural sections never carry a `page` chunk
  themselves — their summaries aggregate from their leaf descendants.

For `page` chunks specifically: one leaf section's body may span one or
more physical pages. A page's text is stored **once**, under whichever leaf
is first in reading order to cover that page (the "owner"). Every other
leaf section that also covers that physical page has, in place of the
text, a literal marker:

  ```text
  [SAME-AS <owner_section_path> p<page_num>]
  ```

  This is a pointer, not a preview. If you need that page's actual text,
  resolve the marker by reading the owner section's chunk at that page
  number — do not treat the marker's absence of text as "this section has
  no content there." `page` chunks also carry `page_nums` (all physical
  pages they cover) and `page_assets` (rendered page-citation screenshots —
  these are references for citation, not separate `image` chunks).

  **Format trap**: `<owner_section_path>` inside the marker is written
  verbatim by the parser and stored as-is — it is the on-disk path
  (`"<source_file_name>/<Heading>/<Heading>/..."`, plain `/`, filename
  included), **not** the DB `section_path` you get back from `outline` /
  `node_filter` / `recall` (which is `" / "`-joined and excludes the
  filename). Do not string-match the marker directly against a DB
  `section_path`. Convert it first — `section_path_from_chunk_path()` in
  `search/lexical_text.py` already does this conversion and is the function
  to reuse when implementing marker resolution, not a new one.

## 3. Asset chunks: `image` / `table`, and `connect_to`

`image` and `table` chunks are **not children of the section they visually
belong to**. In the DB they are parked under their document's synthetic
`Root` section. The real association to a body section is the `connect_to`
list on the **body chunk**, not a location on the asset:

- `relation: "embeds"` — the body chunk that owns/embeds this asset inline.
- `relation: "related"` — another body chunk that shares the same source
  page as a page-track asset, without owning it (`same_as_owner` may name
  the owning section).

This link is **one-directional** (body → asset). There is no stored
asset → body back-link; to find which section(s) an asset belongs to, use
the reverse lookup on the `assets` tool rather than assuming the asset chunk
itself names its host.

## 4. Document graph

Today the graph only has **document-level** nodes (`node_kind='document'`)
and undirected `related` edges between documents. Edge scoring prefers
**typed-entity overlap** between the two documents' aggregated `entities`
first; it only falls back to free-form TF-IDF keyword overlap when either
document lacks entities. Either way the edge carries which terms matched
(`properties.shared_entities` or `properties.shared_keywords`). There are
no section-level or entity-level graph *nodes* — an entity is not itself a
queryable node, and there is no entity-to-entity or entity-to-chunk edge,
only this document-to-document rollup. Use `neighbors` for "what else is
like this document" (and to see which shared terms justify that link), not
for anything finer-grained than a document pair.

## 5. Reserved / not yet available

- **Vector channel**: `recall`'s `channels` parameter reserves a `vector`
  option; it does not exist yet. `recall` today is lexical only.

## 6. Tools and when to use each

| Tool | Use when | Scope | Notes |
|---|---|---|---|
| `list_documents` | Starting cold: which documents exist, what are they about | namespace | Returns per-document keywords/summary/type mix/`parse_track`. |
| `outline` | The task only needs titles/summaries — overview, "what does chapter N cover," picking where to look before reading | one document, or a `section_path` prefix within it | Titles + summaries + `chunk_count`, no body text, no folding. Depth-limited by argument, not by a token budget. Use this to build your own map instead of relying on a pre-folded one. |
| `node_filter` | The task is a traversal/exclusion predicate — FOR ALL / EXISTS / ANY / NOT — over section titles or summaries ("which docs mention X in a heading," "sections NOT about Y") | one or more documents | Deterministic substring/regex match against `section_path` and `summary` only, not body text. Returns the full matching set and count, never a truncated top-K. If the predicate must run against body text, use `grep` instead. |
| `grep` | Exact string / regex / identifier / number lookup that must run against body text | scoped by document/section/chunk_type | Returns match count plus snippets, so ANY/ALL logic can also close over body text, not just titles. |
| `recall` | A fuzzy question where you don't know where the answer lives | namespace or scoped | Ranked candidates (path + content BM25 today; term and vector are separate/reserved — see §5) with path and snippet, not full content. |
| `read` | You already know which section(s)/chunk(s) to read | one or more sections/chunks | Returns full body content, resolves `SAME-AS` markers into the owner's text, expands `connect_to` assets, and converts `page_assets` into URLs. |
| `assets` | You need images/tables directly, or need to find which section(s) host a given asset | one or more documents | Forward (by type/query) and reverse (asset → hosting section) lookup — see §3. |
| `neighbors` | You need related documents in the same namespace | one document | Document-level `related` edges only — see §4. |

General rule: prefer `outline` / `node_filter` to locate before `read`ing
body text; prefer `grep` over `recall` when you know the exact string you
are looking for; only fall back to `recall` for genuinely fuzzy questions.
