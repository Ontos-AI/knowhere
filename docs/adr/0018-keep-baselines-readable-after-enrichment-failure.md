# Keep baselines readable after enrichment failure

## Status

Accepted during the progressive-ingestion planning discussion on 2026-10-26.
The failure-only retry eligibility and retrieval-enhancement wording are also
accepted. Internal API and synchronization details remain implementation work.

## Context

Initial publication and later enrichment have different completion milestones.
A failure after the baseline is readable must not remove that already-published
representation. Users also need a practical way to resume failed background work
after automatic retries are exhausted.

## Decision

Retain the readable baseline after enrichment fails. Track enrichment failure
separately from initial parsing and reader availability. After bounded automatic
retries, expose the failure and allow an authorized user to retry unfinished
processing without uploading again or starting a fresh ingestion charge.

Offer manual retry only after automatic retries have exhausted and processing
has reached a retryable failure. Pending, running, automatically retrying, and
successfully completed work must not expose this action. Retry admission must
check this condition again on the server, since a visible action can become stale.

Describe the background work as retrieval enhancement. The accepted interface
uses an in-progress label, a separate enhancement-failure label, a `Retry` button
only for retryable failure, and a reader notice that confirms the document
remains readable. English copy is recorded in the hardening plan.

## Consequences

Hipohipo needs an action that can operate while the Source remains ready. Its
current parse/synchronization retry path cannot be assumed to cover upstream
enrichment. Knowhere must expose the relevant enrichment state and an authorized,
idempotent retry command. Resume the stage that failed: missing vectors, scope
refinement, final package creation, or downstream synchronization. Successful
scope work and committed snapshots must remain valid. Recovery timing and exact
backend command/state contracts remain in the implementation proposal.
