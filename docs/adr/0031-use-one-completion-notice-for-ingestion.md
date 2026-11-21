# Use one completion notice for ingestion

## Status

Accepted on 2026-11-19. Updated on 2026-11-20 to use coarse chapters as the
first published baseline for progressive PDF and PPTX ingestion and
Parsing Complete as the single successful client-visible state in both modes.

Supersedes [ADR 0011](0011-complete-progressive-enrichment-automatically.md)
and [ADR 0014](0014-notify-both-progressive-completion-milestones.md).

This decision also supersedes the separate public completion milestones
in [ADR 0025](0025-separate-parsing-completion-from-search-readiness.md),
[ADR 0026](0026-publish-search-readiness-from-background-tasks.md), and
[ADR 0028](0028-select-webhook-events-on-a-single-callback-url.md).

## Context

Those decisions treated progressive ingestion as one job with two caller-visible
milestones: a baseline, then automatic enrichment of every scope, including
scopes nobody retrieved. The second event, `job.enrichment.completed`, meant
the fully enriched package was ready.

Retrieval now starts enrichment for a unit it actually returned. A PDF or
sheet unit that has never been retrieved stays on the baseline. Full
enrichment of the document may never happen, so a second job completion has
nothing definite to announce.

The current PDF and PPTX worker publishes page text before chapter recognition.
A later structure task writes coarse chapters and the initial directory. Hipo
includes a Source in Ask after its initial result has synchronized and the
Source is ready. Waiting for coarse chapters gives the first Ask hit a
defined enrichment unit and makes the first result useful for navigation.

PDF and sheet scope recovery works from prior hits and claims. The current
whole-document path for text, Markdown, Word, and standalone audio and video
also [claims pending work from returned retrieval rows](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/jobs/lifecycle/scope_enrichments.py#L399-L475).
Its [recovery check](https://github.com/Ontos-AI/knowhere/blob/51112729e6d1f596f95d4eaf306080b69796722a/packages/shared-python/shared/services/jobs/lifecycle/scope_enrichments.py#L614-L624)
skips untouched pending work and checks whether an outstanding claim can be
recovered.

## Decision

- **Both ingestion modes have one public success state.** Parsing Complete
  means the initial parsed body and its searchable index are available.
  Full ingestion publishes the full body. Progressive ingestion publishes
  the baseline body. Use `done` and `job.completed` for the outcome.
  Search and on-demand enrichment are ongoing capabilities of that Source.
- **Initial index retry reuses durable parsing output.** An initial indexing
  failure can be retried on the same job using the stored result, under the
  billing and eligibility rules in
  [ADR 0029](0029-keep-parsing-charges-and-allow-free-indexing-retries.md).
  Index visibility must be confirmed before reporting Parsing Complete.
- **Progressive PDF and PPTX ingestion first publishes coarse chapters.**
  Compute the chapter ranges before publishing the baseline. Complete
  ingestion after the coarse body and directory are committed and the
  baseline index is searchable. Chapter chunks retain physical page
  or slide references.
- **Progressive PDF and PPTX directory readiness has its own event.** Send
  `job.document.directory_ready` when the initial directory has committed.
  Hipo saves the initial directory during initial result synchronization.
  The directory event and the ingestion completion notice can arrive in
  either order.
- **Tree View refreshes its saved directory when opened.** Hipo renders
  the saved directory and checks for a newer version in a background
  request. The endpoint returns its result directly so the open tree can
  display the new directory as soon as Hipo saves it.
- **Brain moves use the synchronized initial result.** Once the Source is
  synchronized and the target Brain permits the move, the Source can move
  independently of any on-demand enrichment attempts.
- **Enrichment is on demand.** Retrieval of a baseline unit starts enrichment
  of that unit. Units nobody has retrieved stay on the baseline. Enrichment
  does not continue the ingestion job and does not emit
  `job.enrichment.completed`.
- **A unit has one running enrichment.** Its baseline remains eligible for
  retrieval during publication. A successful attempt produces one enrichment
  body for that unit. A later retrieval can start a retry while the unit has
  an attempt remaining.
- **Different units run together.** Two units of the same document can enrich
  and publish independently. Index records become searchable as described in
  [ADR 0030](0030-search-baseline-and-enrichment-without-a-namespace-lock.md).
- **A quiet job does not start enrichment.** Recovery resolves interrupted
  claims and redelivers work already requested by retrieval. Hipo removes
  its separate enrichment retry action.
- **An interrupted attempt uses one attempt.** Recovery records success if
  the body is stored and the full index is searchable as defined in
  [ADR 0030](0030-search-baseline-and-enrichment-without-a-namespace-lock.md).
  For a confirmed incomplete write, failure cleanup removes the partial
  index records. The next retrieval returning the unit's baseline can start
  another attempt while attempts remain. After three failures and cleanup,
  later retrievals leave the unit on its baseline.
- **Report an enrichment attempt failure separately.** Notify Hipo of the
  failed attempt using a dedicated webhook event. Keep the Source ready and
  the ingestion outcome successful.

## Consequences

Callers that waited for `job.enrichment.completed` to refresh a final snapshot
no longer receive that event. The package available at the one notice is the
baseline package for progressive ingestion, and the complete package for full
ingestion. That package stays fixed. Later enrichment does not regenerate it.
Enrichment can change what retrieval returns while publication is underway.
The job's completion notice stays as it was.

The baseline remains eligible throughout a failed enrichment and its cleanup.
A later retrieval that returns that baseline can start another enrichment of
the unit while attempts remain.
