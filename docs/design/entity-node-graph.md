# Chunk-level entity graph (deferred)

**Status:** Deferred — not scheduled, no code written. This replaces the
old "entity 节点 / 共现边：schema 文档预留，发布侧不改" non-goal line in
`.cursor/plans/agentic_corpus_explore_retrieval_c2c4ea21.plan.md` §四 with
an actual future phase (see that plan's todo list for the tracking id).

**Relation to Phase 2 (agent_tools registry)**: does not block or change
it. Reasoning below in "Why this doesn't affect Phase 2."

## Problem

Today `DocumentGraphService.publish_document_graph`
(`packages/shared-python/shared/services/retrieval/graph/service.py:70`)
only ever creates **one graph node per document** and only ever creates
**document ↔ document** `related` edges. The entity/keyword overlap that
drives those edges is computed by first collapsing *every chunk in the
document* into one flat entity set
(`get_normalized_entity_set(chunk_metadata_list)`, `service.py:100`) before
any comparison happens — so by the time two documents get connected, you
have already lost which section/chunk in document A actually shares an
entity with which section/chunk in document B.

The ask: connect the actual **chunks** that share an entity, not just the
two documents they happen to live in.

## What "node" means here (resolved)

A chunk is already, in the overwhelming majority of cases, the
bottom-level unit — one leaf section, one body chunk. The one documented
exception (see `CORPUS_SCHEMA.md` §2, and verified against real published
data: 170 of 801 sections in a two-document local corpus) is **`chunk`
track (text-track) only**: a section that has its own content directly
under its heading, before its first child heading, owns a body chunk of
its own even though it also has children. `page_memory` (page) track has
no such exception — only leaves ever own a body chunk there.

So there is no separate "section-level" option distinct from "chunk-level"
— they are the same thing, because a section owns *at most one* body
chunk. The node for this graph is the chunk: every `text`/`page` body
chunk, and — no reason to special-case them out — every `image`/`table`
chunk too, since they already carry their own `entities` in
`chunk_metadata` independent of which section they are `connect_to`-linked
from. Structural sections that own no chunk of their own simply have no
entities and never become nodes; no rollup needed, no ambiguity.

**Naming collision to keep in mind when this is built**: the Phase 2
`node_filter` tool's "node" means a node in the *section/hierarchy tree*
(operates on `section_path` and `summary`, see `CORPUS_SCHEMA.md` §6). The
graph's "node" (`graph_nodes` table) is a completely different structure —
today only `node_kind='document'` rows, this proposal adds
`node_kind='chunk'` rows. Do not conflate the two when writing tool
descriptions or code comments for whichever tool eventually exposes this
graph.

## Why the DB schema needs no migration

Verified against the actual model
(`packages/shared-python/shared/models/database/document.py:495-575`):

- `GraphNode.node_kind` is a plain `String(32)`, not an enum constrained to
  `'document'` — a new `'chunk'` value needs no schema change.
- `GraphNode.ref_section_id` already exists as a nullable column
  (`document.py:515`) and is already set to `None` for document nodes
  (`service.py:137`) — it is unused, not absent.
- `GraphEdge.source_node_id` / `target_node_id` are plain FKs to
  `graph_nodes.node_id` (`document.py:546-555`) with no constraint that
  both ends share a `node_kind` — a chunk-node ↔ chunk-node edge is already
  legal today, mechanically.

So this is additive: keep the existing `node_kind='document'` nodes/edges
exactly as they are (`neighbors` in Phase 2 keeps querying those, unaffected
— see below), and add `node_kind='chunk'` rows and edges alongside them.

## What actually needs new design (not just "add a node_kind")

1. **Stop collapsing to one set per document.** Index each qualifying
   chunk's own `entities` (`chunk_metadata.entities`, already extracted per
   chunk via `extract_entities_from_chunk_metadata` in
   `graph/keywords.py:53`) as its own node, instead of merging via
   `get_normalized_entity_set` before any comparison.

2. **Replace the O(other documents) peer loop with an inverted index.**
   `service.py:152-161` currently loads every other `node_kind='document'`
   row in the namespace and compares against it — fine at "tens/hundreds
   of documents" scale. At chunk scale (tens of thousands of chunks per
   namespace) this must become an `entity_key → chunk_node_id` lookup
   (a dedicated join table with a plain index beats a JSONB containment
   scan at this volume) so a newly published chunk only compares against
   chunks that already share at least one entity key, not every chunk in
   the namespace.

3. **Retune the overlap threshold — it does not transfer.**
   `graph/keywords.py:7-11`: `MIN_ENTITY_OVERLAP = 2`,
   `MIN_SCORE_THRESHOLD = 0.8`, and `compute_entity_score`
   (`keywords.py:80-96`) is `weight * shared_weight / min(weight_a,
   weight_b)`. This was tuned for whole-document aggregate sets (tens of
   entities). A single chunk typically carries 1-3 entities (verified:
   one real image chunk had exactly 2). Requiring `≥2` shared entities out
   of a 1-3 entity set will rarely fire; and when a tiny set *does* overlap
   by even one entity, the length-weighted score can trivially hit 1.0.
   Neither behavior is useful. Proposed direction: connect on **≥1** shared
   entity, but gate on that entity's **rarity across the namespace**
   (inverse document frequency over chunks, not documents) so a common
   entity (a year, a generic org name) does not wire every chunk to every
   other chunk. This namespace-wide chunk-frequency count does not exist
   today (`compute_tfidf_keywords` in `keywords.py:100` only computes
   document-frequency *within one document's own chunks*, for its
   `top_keywords`, not across the namespace) — it is new infrastructure,
   not a reuse of an existing utility.

4. **Only materialize nodes for chunks that have ≥1 entity.** Chunks with
   an empty `entities` list should not get a `graph_nodes` row at all, or
   the table balloons with rows that can never have an edge.

5. **Recommend keeping this additive, not a replacement.** Leave
   `node_kind='document'` publication in `publish_document_graph` exactly
   as-is; add chunk-level publication as a new, separate write path (can
   live in the same service or a sibling one). Lower risk, and the
   existing document-level `related` edges stay useful for
   `list_documents`/`neighbors`-style "what else is like this document"
   overviews that don't need chunk precision.

## Why this doesn't affect Phase 2

Phase 2's tool list (`agent_tools/registry.py` and the 8 tools in
`CORPUS_SCHEMA.md` §6) touches the graph only through `neighbors`, which is
scoped to `node_kind='document'` `related` edges — exactly what exists
today, unchanged by this proposal. `node_filter`, `outline`, `recall`,
`grep`, `read`, `assets`, `list_documents` never touch `graph_nodes` /
`graph_edges` at all. This entity-node-graph work is a later, additive
phase with its own exposure decision (new tool? extend `neighbors` with a
granularity param? not decided — out of scope until this is scheduled).
