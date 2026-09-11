# Retrieval document scope

Both `POST /api/v1/retrieval/query` and `POST /api/v2/retrieval/query` accept
`include_document_ids` and `exclude_document_ids`.

| Request field | Meaning |
| --- | --- |
| `include_document_ids` omitted or `null` | All otherwise accessible documents in the requested namespace are eligible. |
| `include_document_ids: []` | No documents are eligible; retrieval returns empty results. |
| `include_document_ids: ["doc_a", "doc_b"]` | Only those documents are eligible. |
| `exclude_document_ids: ["doc_b"]` | Exclude these documents, including when they also appear in the include list. |
| `exclude_document_ids` omitted or `[]` | No additional document exclusions. |

For example:

```json
{
  "namespace": "default",
  "query": "What are the findings?",
  "include_document_ids": ["doc_a", "doc_b"],
  "exclude_document_ids": ["doc_b"]
}
```

Only `doc_a` is eligible. IDs must be document IDs, not filenames. Unknown,
foreign-user, and other-namespace IDs do not grant access. Duplicate include
IDs do not broaden the scope. An inclusion list that leaves no eligible
documents returns empty results and does not fall back to the full corpus.

The same boundary applies to small-corpus retrieval, classic search
(`use_agentic: false`), and agent exploration. Agent tools may select a narrower
set of documents, but cannot broaden the request boundary. Final references,
results, and connected asset hydration obey the same scope. Cache entries
distinguish unrestricted, empty, and explicitly included document sets.

Scope restricts available evidence; it does not add an LLM routing step or
change path filtering, ranking, or threshold semantics. Omitting both fields
preserves unrestricted retrieval within the existing user/namespace boundary.
