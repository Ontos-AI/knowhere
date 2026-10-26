# Architecture Decision Records

This directory records durable architecture decisions for Knowhere. Keep ADRs
short, repo-facing, and focused on decisions future maintainers or agents might
otherwise re-litigate.

Use this shape:

- Status
- Context
- Decision
- Consequences

## Index

| ADR | Title |
| --- | --- |
| [0001](0001-keep-routes-and-worker-tasks-as-adapters.md) | Keep routes and worker tasks as adapters |
| [0002](0002-use-typed-workflow-outcomes.md) | Use typed workflow outcomes |
| [0003](0003-keep-retrieval-workflow-policy-explicit.md) | Keep retrieval workflow policy explicit |
| [0004](0004-anonymous-self-hosted-telemetry.md) | Anonymous self-hosted telemetry |
| [0005](0005-stream-retrieval-progress-over-sse.md) | Stream retrieval progress over SSE |
| [0006](0006-atomically-publish-retrieval-serving-index.md) | Atomically publish the retrieval-serving index |
| [0007](0007-use-coherent-retrieval-serving-generations.md) | Use coherent retrieval-serving generations |
| [0008](0008-use-a-maintenance-window-for-serving-index-rollout.md) | Roll out the serving index online |
| [0009](0009-use-token-leading-covering-index-for-map-unit-lookup.md) | Use a token-leading covering index for map-unit lookup |
| [0010](0010-backfill-serving-index-statistics-in-place.md) | Backfill serving-index statistics in place |
| [0011](0011-complete-progressive-enrichment-automatically.md) | Complete progressive enrichment automatically |
| [0012](0012-publish-a-viewable-baseline-before-enrichment-completes.md) | Publish a viewable baseline before enrichment completes |
| [0013](0013-refresh-readers-once-after-full-enrichment.md) | Refresh readers once after full enrichment |
| [0014](0014-notify-both-progressive-completion-milestones.md) | Notify both progressive completion milestones |
| [0015](0015-retain-baseline-and-final-citation-snapshots.md) | Retain baseline and final citation snapshots |
| [0016](0016-fail-retrieval-on-required-query-embedding-errors.md) | Fail retrieval on required query embedding errors |
| [0017](0017-publish-viewable-baselines-with-missing-page-vectors.md) | Publish viewable baselines with missing page vectors |
| [0018](0018-keep-baselines-readable-after-enrichment-failure.md) | Keep baselines readable after enrichment failure |
| [0019](0019-reuse-ten-minute-hipo-enrichment-reconciliation.md) | Reuse ten-minute Hipo enrichment reconciliation |
| [0020](0020-check-visible-hipo-enhancement-status-every-fifteen-seconds.md) | Check visible Hipo enhancement status every fifteen seconds |
| [0021](0021-gate-final-enrichment-completion-on-all-required-work.md) | Gate final enrichment completion on all required work |
