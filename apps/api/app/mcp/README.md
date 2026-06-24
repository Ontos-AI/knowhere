# Knowhere MCP Server

This module exposes the server-side MCP surface for inspecting published Knowhere
documents. The API app mounts the server at `/mcp`; the implementation lives in
`retrieval_server.py`.

## Request Model

- Authentication uses the same `Authorization` header path as the public API.
- Namespace resolution is explicit tool `namespace` argument first, then the
  `x-knowhere-namespace` header, then the default retrieval namespace.
- Tools operate only on active documents in the authenticated user's namespace.
- Responses are structured MCP outputs, not JSON embedded only in text.

## Tools

### `knowhere_search`

Search published documents in a namespace and return evidence for follow-up
inspection. The response includes `evidence_text`, `referenced_chunks`,
`decision_trace`, and ranked result previews.

Use this first when the target document is unknown.

### `knowhere_list_documents`

List active documents in the effective namespace.

### `knowhere_get_document_outline`

Return metadata, chunk counts, type counts, and ordered section outlines for one
active document revision. Outline responses prefer the persisted MCP outline
snapshot when it is valid, then fall back to live SQL aggregation.

### `knowhere_read_chunks`

Read bounded exact chunks from one active document by:

- exact `section_path`
- 1-based `start_chunk` / `end_chunk`
- canonical `document_chunk_id`
- semantic parser `chunk_id`

Reads use database positions and are hard-capped by the service. Media chunks
(`image` and `table`) include a fresh `asset_url` when the chunk has a valid
`images/...` or `tables/...` artifact path. Result artifact URLs default to a
7-day presigned lifetime, subject to the configured storage provider and signer
credentials.

### `knowhere_grep_chunks`

Search chunks inside one active document by literal text or regular expression.
Grep is scoped to one document, applies chunk type and section prefix filters in
SQL before limiting, and returns ordered snippets plus exact follow-up IDs.

## Non-Goals

This MCP surface is retrieval and inspection only. It does not expose ingestion,
upload, job management, or upload confirmation tools.

## Maintenance Notes

- Keep tool output models typed with Pydantic models so MCP clients receive
  structured schemas.
- Update `apps/api/tests/contract/test_mcp_retrieval_contract.py` when adding,
  removing, or renaming tools or output fields.
- Avoid routing MCP inspection through REST handlers; use the service and
  repository layers directly so MCP behavior can stay focused and bounded.
