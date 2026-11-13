# Activate document revisions only after search readiness

- Status: Accepted for the planned OpenSearch migration. An isolated working
  implementation exists; production cutover and acceptance remain open.
- Engineering reference (2026-11-03): The selected
  [implementation design](../design/opensearch-engineering-v1.md) specifies
  pointers, guarded activation, per-execution physical builds, and repeated
  retired-build cleanup. Its reader-protection requirements are superseded by
  the accepted 2026-11-04 replacement-race decision below. That amendment passed
  targeted isolated checks; production fault/quality acceptance remains open.
  See the [validation report](../validation/opensearch-unlocked-reads-2026-11-04.md).
- Context: A document can have a searchable revision while a replacement
  source file or a new parse produces another revision. Background indexing
  may be delayed or fail, so selecting the replacement at parsing completion
  would interrupt retrieval of an otherwise searchable document.
- Decision: Keep the published revision selected for default retrieval until
  its replacement is fully searchable and activated. The old revision is
  served from OpenSearch, never through PostgreSQL lexical fallback.
  Only switch default retrieval to the replacement after full
  indexing success, visibility acknowledgment, and the corresponding revision
  activation and cache-coherence steps. Indexing failure leaves the previous
  published revision selected. The replacement's completed parsing result
  remains independently accessible through its parsing workflow.
- Publication protocol (accepted 2026-11-02): Use PostgreSQL publication
  metadata as the authority for retrieval revision selection. Prepare the
  candidate and confirm visibility in OpenSearch first, outside PostgreSQL
  transactions. In one guarded PostgreSQL transaction, switch the published
  revision, advance internal retrieval-cache generation, mark indexing and
  overall completion, and persist the subscribed completion outbox event.
  Retrieval uses a matching revision/generation snapshot for cache acceptance,
  OpenSearch candidates, and hydration. Delayed Redis invalidation cannot
  make an obsolete cache entry current. After interruption, inspect actual
  committed state: retain old selection if unpublished; retain new selection
  and deliver the original event if committed. Coordinate eligibility with
  archival, supersession, and current-attempt ownership. Concrete storage,
  lock ordering, and cache changes remain implementation work in the
  [publication protocol](../design/opensearch-publication-readiness.md#accepted-revision-publication-protocol).
- Default namespace scope: Search the published revisions only. A newly
  added document without a published revision is not included until ready
  and activated, and does not block retrieval of other published documents.
  Do not add pending-document notices, warnings, or completeness hints to
  ordinary retrieval responses. Document this behavior and expose readiness
  through the separate parsing and retrieval states. Overall completion
  continues to guarantee search readiness at the completion milestone.
- Retrieval scope clarification (accepted 2026-11-02): Keep the existing
  retrieval query API. Document allowlists narrow the same published scope;
  documents without a published revision contribute no candidates, and an
  empty eligible scope returns the normal empty result. Readiness is
  observable through job states, SDK waiting, and webhook events. The earlier
  explicit-version search branch and proposed not-ready HTTP 409 response
  are withdrawn. Do not add a public search revision selector in this migration.
- Document-content selection (accepted 2026-11-02): Default document-content
  reads use the latest successfully parsed revision independently of indexing.
  This is distinct from the published revision selected for default retrieval.
  Retrieval references bind the retrieved revision for subsequent content,
  asset, and citation reads; the service and SDK handle that binding for
  ordinary clients. Both selections share immutable revision data; use the
  separate latest-parsed and published pointers in the
  [engineering design](../design/opensearch-engineering-v1.md#persistence-and-identities).
  See ADR 0025 for the client contract; implementation/verification remain work.
- Document admission (accepted 2026-11-02): Retain one active job for the same
  document through parsing and indexing, including an active explicit retry.
  Reject another update with HTTP 409 and identify the active
  job until overall search-ready completion or terminal failure. Parsing
  completion, parsing-result access, and an early parsing-only SDK return do
  not release this restriction. Other documents remain independently eligible
  for concurrent processing. Indexing-only retry must coordinate with this
  rule and the supersession policy below.
  Owner-confirmed example: after a new revision's job is accepted and while
  it is parsing, retrying the older failed revision's index returns 409.
  Do not defer that retry until the new job ends. If an eligible old retry
  wins admission first, the concurrent new job conflicts instead. This is
  shared document-level admission, not creation of a new job for index retry.
  Owner-confirmed simplification: a new-version upload also returns 409 when
  the old job is still nonterminal but its indexing deadline has elapsed.
  Upload does not settle the old job; another recovery path must first commit
  its done/failed outcome. Keep explicit indexing retry's expired-state checks
  separate from this deliberately simpler new-job admission rule.
  See the
  [admission policy](../design/opensearch-publication-readiness.md#accepted-same-document-admission-through-indexing).
- Archival during indexing (accepted 2026-11-02): Allow immediate archival
  after parsing completes while indexing is pending or running. Subsequent
  retrieval excludes the document, and delayed workers cannot reactivate or
  publish it. An unfinished job ends with overall/indexing failure, parsing
  completed, and an archival explanation. Retain its parsing result and
  charge; notify through subscribed `job.failed` events. Reject indexing
  retries for archived documents. Jobs completed before archival retain
  their historical `done` and completed phase states. Coordinate archival
  and activation atomically: the committed ordering determines whether
  archival blocks publication or follows completed publication. Physical
  cleanup need not block logical exclusion. See the
  [archival policy](../design/opensearch-publication-readiness.md#accepted-archiving-during-indexing).
- Superseded candidates (accepted 2026-11-02): A later durably accepted update
  to the same document supersedes an older failed candidate. Reject its
  indexing-only retry with HTTP 409 and retain its parsing result under the
  existing retention policy. Supersession occurs at acceptance, even before
  the later update is parsed or published; a rejected update does not
  supersede the candidate, and a later failure does not automatically revive
  its eligibility. Fence old task deliveries at activation so they cannot
  publish a superseded candidate. Eligibility checks and the publication
  decision must be coordinated with new-update acceptance. This does not
  change which already published revision serves while a replacement is
  prepared. Implement the accepted-job, attempt/owner, build and backend-epoch
  guards in the [activation protocol](../design/opensearch-engineering-v1.md#atomic-activation)
  and verify them against the [supersession policy](../design/opensearch-publication-readiness.md#accepted-superseded-candidate-retry-policy).
- Old-revision cleanup (accepted 2026-11-02): After replacement activation,
  remove the old revision's lexical records in the background. The original
  requirement to wait for existing readers is superseded by the 2026-11-04
  amendment below. Requests selecting versions after activation use the new publication.
  Revision cleanup retains the namespace index and the new revision. Do not
  remove the published old revision while replacement indexing is pending or
  failed. Retain parsing results, body data, assets, and citations under the
  existing retention rules; returned references remain bound to their parsed
  revision independently of the lexical records. Knowhere handles this
  coordination and reference-link renewal for ordinary clients. Exact
  cleanup mechanisms remain implementation work in the
  [cleanup policy](../design/opensearch-publication-readiness.md#accepted-old-revision-lexical-cleanup).
- In-flight replacement (accepted 2026-11-04): Cleanup does not wait for
  requests that selected the old build. If native search then returns fewer
  or no hits for that document, return the actual results with normal HTTP
  200, including an empty result list. Do not retry because of replacement,
  reselect the new revision to fill gaps, or return an updating/conflict
  error. Keep each returned hit's body, assets and citation bound to its
  selected immutable revision/build. No final publication-drift check is
  required solely to detect and restart this race. Actual backend failures
  remain errors/recovery cases; LRU reopening is a separate policy. A later
  request selects the current publication normally. Retired-build cleanup
  still targets an exact, non-reusable build ID and never deletes the current
  published build or makes a retired build eligible again. See the
  [revised coordination proposal](../design/opensearch-concurrent-read-lock-proposal-2026-11-04.md#accepted-replacement-behavior-and-proposed-smaller-coordination-protocol).
- Ranking boundary (accepted 2026-11-03): Native Lucene statistics include
  candidate and obsolete records physically present in the namespace index.
  Publication filters control eligible hits; they do not recalculate BM25
  statistics over published records alone. Preserve correct revision/body/
  reference selection without promising ranking stability during staging or
  cleanup. Apply the business-evidence quality gate before, during, and after
  partial writes, candidate failure/retry, activation, and lexical cleanup.
  See the [ranking boundary](../design/opensearch-publication-readiness.md#ranking-statistics-during-revision-coexistence).
- Consequences: The latest parsed revision and the published revision may
  differ. Publication must distinguish candidate revision data from the
  revision selected for retrieval, including PostgreSQL result hydration.
  These are revisions of one stable document identity, not independent
  documents or different backend choices. An OpenSearch node outage still
  makes its old revision unavailable. Implementation and fault validation
  of the accepted publication protocol, concrete stale-task fencing,
  concurrent-read simplification, and cleanup scheduling remain
  follow-up implementation topics. This does not establish a new
  historical-version retention period or a public revision-selection API.
