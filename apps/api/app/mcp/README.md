# Knowhere MCP Server

Knowhere exposes a server-side MCP endpoint at `/mcp` for agents that need to
parse remote documents, track parsing jobs, search published knowledge, and read
the exact chunks behind a search result.

Use this MCP server when the agent should work through Knowhere's authenticated
document store. Do not use it for local files on the client machine; a remote MCP
server cannot read the client's filesystem.

## Connect

Send the same bearer token used by the public Knowhere API:

```text
Authorization: Bearer <knowhere-api-key>
```

Namespace can be supplied in either place:

```text
x-knowhere-namespace: <namespace>
```

or as a `namespace` argument on each tool. The explicit tool argument wins over
the header. If neither is provided, Knowhere uses the default namespace.

All tools are scoped to the authenticated user and the effective namespace.
Responses use MCP structured output, so clients should read `structuredContent`
rather than parsing JSON from text.

## Typical Workflow

1. Parse a remote file with `knowhere_parse_url`.
2. Poll `knowhere_get_job_status` until `is_terminal` is true.
3. Discover documents with `knowhere_list_documents` or `knowhere_search`.
4. Inspect the document with `knowhere_get_document_outline`.
5. Read exact chunks with `knowhere_read_chunks`.
6. Use `knowhere_grep_chunks` when you need grep-style literal or regex matching
   inside one known document.

## Tools

### `knowhere_parse_url`

Starts a background parse job for a remote `http` or `https` document URL.

Inputs:

- `url`: remote document URL.
- `namespace`: optional namespace override.
- `document_id`: optional existing document ID for update flows.
- `data_id`: optional caller correlation ID.
- `parse_track`: parser track, usually `chunk`.
- `parsing_params`: optional parser settings.

Output:

- `namespace`: effective namespace.
- `job`: created job, including `job_id`, `status`, `source_type`, `data_id`,
  and related job fields.
- `interpretation`: short instruction for what to do next.

After this tool returns, poll `knowhere_get_job_status` with the returned
`job.job_id`.

### `knowhere_get_job_status`

Reads one parse job owned by the authenticated user.

Inputs:

- `job_id`: job ID returned by `knowhere_parse_url`.
- `namespace`: optional namespace override.

Output:

- `namespace`: effective namespace.
- `job`: job result payload, including `status`, `document_id`, `result_url`,
  `error`, and timing fields when available.
- `is_terminal`: true when the job no longer needs polling.
- `is_success`: true when `status` is `done`.
- `is_failure`: true when `status` is `failed`.
- `interpretation`: `completed`, `still running`, or failure guidance.

Only `done` and `failed` are terminal. A job with unchanged progress is not
automatically stuck.

### `knowhere_search`

Searches published documents in a namespace and returns evidence plus exact
references for follow-up inspection.

Use this first when the agent does not know the target `document_id`.

Inputs:

- `query`: natural language or keyword query.
- `namespace`: optional namespace override.
- `top_k`: maximum initial discovery candidates.
- `target_content`: `all`, `text`, `image`, `table`, `text_image`, or
  `text_table`.
- `signal_paths`: optional section/path signals.
- `filter_mode`: `keep` or `delete` for `signal_paths`.
- `threshold`: minimum retrieval score threshold.
- `exclude_document_ids`: document IDs to exclude.
- `exclude_sections`: `{document_id, section_path}` items to exclude.

Output:

- `namespace`
- `query`
- `evidence_text`
- `referenced_chunks`
- `decision_trace`
- `results`
- `stop_reason`
- `failure_reason`

Use `referenced_chunks` and `results` to get exact `document_id`,
`document_chunk_id`, `chunk_id`, and `section_path` values for later reads.

### `knowhere_list_documents`

Lists active documents in the effective namespace.

Inputs:

- `namespace`: optional namespace override.

Output:

- `namespace`
- `documents`: active documents with `document_id`, `source_file_name`,
  `status`, current revision ID, and timestamps.

### `knowhere_get_document_outline`

Returns a document's active revision outline.

Inputs:

- `document_id`: exact Knowhere document ID.
- `namespace`: optional namespace override.

Output:

- `namespace`
- `document`
- `job_result_id`
- `job_id`
- `total_chunks`
- `type_counts`
- `sections`
- `section_tree`

Each section includes `section_id`, `section_path`, `section_title`,
`section_level`, `summary`, `start_chunk`, `end_chunk`, `chunk_count`, and
`type_counts`.

`sections` is a flat ordered list for scanning and range reads. `section_tree`
contains the same section fields nested by document hierarchy. Use the returned
`section_path`, `start_chunk`, and `end_chunk` values to make bounded calls to
`knowhere_read_chunks`.

### `knowhere_read_chunks`

Reads exact chunks from one active document.

Inputs:

- `document_id`: exact Knowhere document ID.
- `namespace`: optional namespace override.
- `section_path`: optional exact section path from the outline or search result.
- `start_chunk`: optional 1-based chunk position to start reading.
- `end_chunk`: optional 1-based chunk position to stop reading.
- `document_chunk_id`: optional canonical `document_chunks.id`.
- `chunk_id`: optional parser semantic chunk ID.

Selection guidance:

- Prefer `document_chunk_id` for an exact follow-up from search or grep.
- Use `section_path` plus a chunk range for outline-based reading.
- Use `chunk_id` only when semantic duplicate chunks are acceptable or the
  caller also has enough context to disambiguate.

Output:

- `namespace`
- `document_id`
- `job_result_id`
- `job_id`
- `chunks`
- `next_chunk`

Each chunk includes `position`, `document_chunk_id`, `chunk_id`, `chunk_type`,
`content`, `section_path`, `source_chunk_path`, `file_path`, `asset_url`,
`sort_order`, and `metadata`.

The default read window is 12 chunks. The hard cap is 40 chunks. If more chunks
are available, `next_chunk` tells the caller where to continue.

Image and table chunks include a fresh `asset_url` when Knowhere can resolve the
stored asset path. Asset URLs are presigned result artifact URLs and currently
use a 7-day lifetime.

### `knowhere_grep_chunks`

Searches inside one known document, similar to command-line grep. This is for
literal or regex matching against chunk text, not semantic retrieval.

Inputs:

- `document_id`: exact Knowhere document ID.
- `pattern`: literal text by default, or regex when `is_regex` is true.
- `namespace`: optional namespace override.
- `is_regex`: treat `pattern` as a regular expression.
- `is_case_sensitive`: use case-sensitive matching.
- `max_results`: maximum matches to return.
- `chunk_type`: optional filter such as `text`, `image`, or `table`.
- `section_path_prefix`: optional section prefix filter.

Output:

- `namespace`
- `document_id`
- `job_result_id`
- `job_id`
- `matches`
- `truncated`
- `scanned_chunks`

Each match includes `position`, `document_chunk_id`, `chunk_id`, `chunk_type`,
`section_path`, `source_chunk_path`, `file_path`, byte offsets, and a context
snippet.

The default result limit is 20 matches. The hard cap is 50 matches. Literal grep
is pushed into SQL. Regex grep is bounded with database and regex timeouts.

## What Is Not Exposed

The MCP server does not expose local upload, upload confirmation, job queue
internals, or direct S3 plumbing. Direct upload URLs and `confirm-upload` remain
client/API implementation details.

For MCP clients, the supported ingestion path is:

```text
knowhere_parse_url -> knowhere_get_job_status -> inspection/search tools
```

## Maintainer Notes

- Keep tool outputs typed with Pydantic models so MCP clients receive structured
  schemas.
- Update `apps/api/tests/contract/test_mcp_retrieval_contract.py` when adding,
  removing, or renaming tools or output fields.
- Keep MCP inspection on service/repository helpers rather than REST handlers so
  tool behavior remains bounded and server-side.
