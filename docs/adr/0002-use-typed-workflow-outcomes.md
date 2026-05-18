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

Boolean and tuple facades may remain only where there is a real public contract.
The implementation should concentrate reason-bearing behavior behind typed
outcomes.

## Consequences

Contract tests can assert stable behavior and failure reasons without reaching
into private helper call order. Avoid internal-only compatibility facades; if
there is no external consumer, update callers to the real module boundary.
