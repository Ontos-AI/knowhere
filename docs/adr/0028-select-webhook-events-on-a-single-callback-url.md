# Select webhook events on a single callback URL

- Status: Accepted for the planned OpenSearch migration; formal service/SDK
  and production acceptance remain open.
- Evidence/delivery distinction (2026-11-04): The
  [progress ledger](../design/opensearch-implementation-progress.md) records
  isolated candidate/component evidence and its limits, not completion of every
  requirement in this ADR. Use the
  [development plan](../design/opensearch-development-plan.md) for remaining
  service, SDK, target-environment and release gates.
- Engineering reference (2026-11-03): The selected
  [wire contract](../design/opensearch-engineering-v1.md#webhook-snapshots)
  specifies event identity projection, phase snapshots, original milestone
  timestamp fields, and revision-bound result delivery. Earlier statements
  leaving those shapes open defer to that reference; SDK/webhook acceptance
  remains future work.
- Context: Clients may need parsing completion, overall search-ready
  completion, or both milestones. Existing API and SDK webhook configuration
  accepts one URL, and existing clients expect terminal notifications.
- Decision: Preserve `webhook.url` and add an optional `webhook.events` list.
  One callback URL receives the selected event types, with each milestone
  represented by its own event and payload. An explicit event list selects
  the callbacks to send; selecting parsing completion alone does not
  implicitly subscribe to overall completion. Omitting `events` preserves
  the existing terminal-notification behavior (`job.completed` and
  `job.failed`), without automatically opting clients into new phase events.
  Do not require a separate URL for each event type in the first version.
- Subscription validation (accepted 2026-11-02): An explicit `events` value
  must be a nonempty list containing only the exact names `job.parsed`,
  `job.completed`, and `job.failed`. Deduplicate repeated names. Reject an
  empty list, a non-list value, or any unknown name using the existing
  parameter-error contract before job creation or parsing charges. Reject
  mixed valid/unknown lists rather than silently dropping unknown entries.
  Omission preserves terminal-event subscriptions when a URL is configured;
  omitting webhook configuration does not create a callback. Subscription
  deduplication does not guarantee ordered or exactly-once delivery. See the
  [validation contract](../design/opensearch-publication-readiness.md#accepted-events-validation).
- Parsing event (name accepted 2026-11-02): Use `job.parsed` when the job's
  immutable parsing result is durable and usable for reading or download.
  Bind result access to that job's revision. This event does not indicate
  overall job completion or search readiness. If subscribed to both success
  milestones, create distinct `job.parsed` and `job.completed` events even
  when indexing finishes before delivery. Indexing-only retry does not
  create another parsing-completion event; ordinary redelivery of an
  existing event remains possible under the same event identifier.
  A caller may finish its parsing-only business workflow on this event, but
  the payload remains a parsing milestone with the actual event-time job/phase
  state; do not rename it `job.completed` or project overall success early.
  Subscription/delivery never marks the job terminal or excludes it from
  indexing timeout settlement. A default SDK wait still requires overall
  success unless that invocation explicitly requests the parsed target.
- Failure event: Use `job.failed` for both parsing failure and indexing
  failure. Include `failure_stage` with the value `parsing` or `indexing`,
  plus the accepted `parsing_status` and `indexing_status` fields and values
  for that document revision, as defined in ADR 0025.
  An indexing failure preserves the successful parsing state and its result.
  Clients subscribing to `job.failed` can distinguish the failed phase from
  the payload. Do not create separate phase-specific failure event types.
- Event timing and terminal states (accepted 2026-11-02): Do not add a
  client-facing status-version counter or promise webhook delivery order.
  Use the original server-side event occurrence time and existing event ID;
  keep both unchanged when redelivering the same event. The payload describes
  the state at event creation, while the job query exposes current state.
  Timestamps support inspection and client handling, not a strict ordering
  guarantee. Once a job reaches overall completion, no late task or failure
  notification may move it to failed or active processing. Indexing failure
  ends the current execution; an eligible explicit retry may start another
  indexing attempt under the same job. Preserve historical failure events.
  Reuse the existing completion/failure event types for retry outcomes and
  do not generate another parsing-completion event without a new parse.
  Use the defined `parsed_at`, `completed_at` and `failed_at` milestone fields
  and `event_id` projection from the
  [webhook snapshot contract](../design/opensearch-engineering-v1.md#webhook-snapshots).
- Consequences: Existing URL-only callers retain their notification contract,
  while new callers can receive multiple milestones at the same endpoint and
  route by the payload's `event` field. API and SDK request models must gain
  event-selection support. Webhook selection is independent of the SDK waiting
  target accepted in ADR 0025: selecting a callback event does not change when
  `parse()` returns. Its default remains overall search-ready completion;
  callers can explicitly select the parsing-only return milestone. Overall
  completion still requires search readiness. Phase-state field names and
  values are accepted in ADR 0025. Preserve the existing terminal webhook
  `status` vocabulary, including `completed` for `job.completed`; do not
  rename it to the job-query API's `done` value as part of this change.

The shape below selects both accepted success milestones. `job.completed`
retains its overall-completion meaning.

```json
{
  "webhook": {
    "url": "https://client.example/knowhere",
    "events": ["job.parsed", "job.completed"]
  }
}
```
