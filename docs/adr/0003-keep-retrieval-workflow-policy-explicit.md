# 0003 Keep Retrieval Workflow Policy Explicit

## Status

Accepted

## Context

Retrieval requests include scope, document/section exclusions, data type,
signal paths, channel selection, channel weights, internal recall, and ranking
policy. When these fields are passed as loose keyword arguments, the legacy and
agentic routes can drift.

## Decision

Retrieval should keep request policy in typed request modules. `RetrievalQuery`
owns cache and route policy. `WorkflowRunRequest` and `WorkflowStepRequest`
carry the agentic workflow projection of that policy through planning and step
execution.

Fields that are accepted but not yet implemented in a route, such as reranking
or threshold semantics for multi-step workflow answers, must remain explicit in
the request type rather than disappearing silently.

## Consequences

The request type becomes the test surface for retrieval policy. Product
behavior changes such as enabling workflow reranking or threshold filtering
should be separate decisions with contract tests for the selected phase.
