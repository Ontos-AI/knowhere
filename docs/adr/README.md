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
