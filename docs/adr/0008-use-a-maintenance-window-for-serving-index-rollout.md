# Roll out the serving index online

- Status: Accepted
- Context: The server and document publication must remain available while serving-index schema changes and backfill are introduced.
- Decision: Use additive online migrations and bounded idempotent backfill. The retrieval reader automatically uses the serving index only when a revision is complete and consistent; otherwise it uses the exact legacy reader. New publication continues online and builds serving data atomically before activating a revision. Do not expose partial serving data.
- Consequences: There is no planned retrieval or publication downtime. Backfill consumes bounded database resources and some revisions remain on the slower legacy path until complete. Generation checks and stale-revision guards are required while backfill and publication run concurrently.
