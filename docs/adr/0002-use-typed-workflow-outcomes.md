# 0002 Use Typed Workflow Outcomes

## Status

Accepted

## Context

Several Knowhere workflows used tuples, dictionaries, or booleans to represent
rich behavior. That made retry, fallback, and transition decisions harder to
reason about because callers lost the reason behind a result.

## Decision

New or deepened workflow seams should use typed outcomes when callers need more
than a yes/no answer. Current examples are `JobTransitionOutcome`,
`WorkloadEstimate`, `ParseOutput`, `ParseArtifact`, `GeneratedResultPackage`,
and `PostCommitEffectPlan`.

Boolean and tuple facades may remain where they are public compatibility
interfaces, but the implementation should concentrate reason-bearing behavior
behind typed outcomes.

## Consequences

Contract tests can assert stable behavior and failure reasons without reaching
into private helper call order. Compatibility wrappers should be kept small and
named as wrappers, not used as the primary implementation seam.
