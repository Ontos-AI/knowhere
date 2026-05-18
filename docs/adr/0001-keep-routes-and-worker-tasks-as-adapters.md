# 0001 Keep Routes And Worker Tasks As Adapters

## Status

Accepted

## Context

Knowhere has HTTP routes, internal callback routes, and Celery tasks that start
workflows. Bugs become harder to localize when these adapters also own parser,
publication, billing, retrieval, or webhook implementation details.

## Decision

Routes and worker tasks should translate external inputs into application or
shared workflow calls. Domain behavior should live behind workflow modules such
as Document Ingestion, Worker Document Parsing, Publication, Retrieval, Billing
Workflow, Storage Event Intake, and Webhook delivery.

## Consequences

Adapters stay thin and contract tests can assert public behavior at HTTP,
worker-task, database, storage, Redis, or event surfaces. New feature work
should prefer deep workflow modules over adding branch-heavy logic to routes or
Celery task functions.
