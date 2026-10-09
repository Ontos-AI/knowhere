# Evidence sufficiency review

Set `review_evidence: true` on a v1 or v2 retrieval query to independently assess
whether the selected text and table evidence supports the original question.
The default is `false`, which adds no reviewer calls.

```json
{
  "namespace": "default",
  "query": "Compare the standard and enterprise plans' prices and storage limits.",
  "use_agentic": true,
  "review_evidence": true
}
```

The feature applies to the `agent_explore` route with either the OpenAI-compatible
or Cursor harness. The classic and small-corpus shortcuts return an `unverified`
assessment when requested; they do not invoke a reviewer or change routes.
An empty query still short-circuits retrieval.

## Behavior

When the explorer requests `finish`, the service resolves the picked references
using the request's document scope and pinned revisions. It applies the same
section/type exclusions and asset composition used for the returned evidence.
The short database transaction closes before the review model is called.

A separate, tool-free model call receives the immutable original question and
the actual composed text/table evidence. Executor reasoning, finish notes and
candidate summaries are excluded. Its assessment names the required facets,
marks each supported, missing or conflicting, and cites exact text spans from
known evidence IDs. The parser checks the schema and citations. A second review
must preserve all the first review's facet IDs and requirements.

An insufficient assessment can request one corrective continuation in the same
exploration episode. The original question, evidence pool, read/pick rules,
authorization and revision pins are preserved. The explorer receives only the
missing/conflicting requirements as feedback. After that continuation, a second
finish is reviewed and the episode ends even if evidence is still incomplete.

Before responding, the service composes the final evidence again. Changes to
its content, provenance or full packet fingerprint invalidate the assessment.
The service continues to return evidence: `answer_text` remains empty.

## Response

The optional `evidence_review` field includes:

| Field | Meaning |
| --- | --- |
| `status` | `sufficient`, `insufficient`, or `unverified` |
| `reason` | Short explanation of the assessment or verification limitation |
| `coverage` | Required facets, their support status, and exact cited text spans |
| `sources` | Mapping from evidence IDs to document/chunk/revision/section/page provenance |
| `attempts`, `repairs` | Reviewer calls attempted and corrective continuations granted |
| `reviewer_tokens` | Known tokens consumed by the reviewer |
| `reviewer_usage_complete` | Whether all reviewer token usage is known |
| `episode_tokens`, `usage_complete` | Known total episode tokens and whether the total is complete |
| `evidence_fingerprint` | SHA-256 binding the assessment to the original question and final evidence |

The evidence-review trace is distinct from an explorer's `finish` request.
Budget exhaustion and missing finish calls are recorded as stops, not fabricated
successful finishes. Reviewed and unreviewed requests use distinct cache keys.
Unverified assessments are not cached, so temporary reviewer failures can recover.

`insufficient` describes the selected evidence. It does **not** establish that
the corpus contains no answer. A caller should inspect missing facets or ask a
follow-up instead of treating the status as a factual answer.

## Limits and provider behavior

- At most two reviewer calls and one corrective continuation per episode.
- Reviewer calls share the original exploration step and wall-clock budgets.
  After the first insufficient review, at most four remaining steps are allowed;
  these include subsequent exploration turns and the second reviewer call.
  The existing pick-phase accounting is unchanged.
- Each review has a 30-second ceiling, bounded further by the remaining episode
  time, including evidence loading. Input is limited to 64 KiB of fully serialized
  messages and output to 1,024 tokens. Oversized input is unverified, not silently
  truncated into a passing result.
- Reviewer calls use the request-scoped OpenAI-compatible text provider and a
  separate async request with retries disabled. Cursor remains the explorer when
  selected; its model does not determine the text review model.
- Direct text providers, including request-scoped BYOK configuration, are supported.
  Pooled-only and mock text backends return unverified; the feature does not bypass
  their routing or silently fall back to another credential.
- The first version reviews text and composed table HTML. Outlines alone are not
  factual support. Selected page/image evidence or other nontext parts remain
  unverified because this reviewer does not inspect visual content.
- Missing usage is reported as unknown. Known totals then represent a lower bound.
  Cancelling an existing synchronous explorer or storage call can leave that
  operation running until its own transport timeout; late results cannot alter
  the finalized reviewed pool.

A model can still omit a facet or misinterpret a real quotation. Exact-span and
schema checks establish provenance and internal consistency, not semantic truth.
The controlled experiment includes an intentionally wrong, schema-valid judge
assessment so this limitation remains visible.

## Validation

See [the reproducible experiment](evidence-sufficiency-experiment.md) for the
paired scenarios, metrics, negative control and optional live-review mode. The
PR's CI runs the shared review tests, controlled experiment, API contracts and
existing worker contracts. Live-model quality gains require a separately
reported evaluation with actual provider credentials.
