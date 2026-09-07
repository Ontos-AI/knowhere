# Retrieval serving snapshot cache

Map-nav reads a namespace routing snapshot that contains section/chunk
relationships, ordering, chunk types, connection references, and remount
ownership. It intentionally does not contain chunk body text; final hydration
still reads pinned revision content from PostgreSQL.

## Persisted formats

- Namespace snapshots written by current publication code use format version 2
  and contain routing metadata only.
- The reader remains compatible with version 1 snapshots so existing rows can
  be served during rollout. No unconditional namespace rebuild is required
  solely because the reader was upgraded.
- A missing, corrupt, stale, or generation-mismatched snapshot falls back to
  the exact manifest/table loading path.

## Redis cache

The map-nav reader caches the compressed snapshot bytes, not a decoded Python
dictionary. Keys are scoped by user, normalized namespace, and serving
generation:

```text
retrieval:snapshot:v2:{user_id}:{namespace}:g{generation}
```

The cache TTL is one hour. Redis errors, misses, and invalid blobs are
non-fatal: PostgreSQL remains the source of truth and the reader repopulates
Redis after a successful database read. Binary snapshot operations use a Redis
connection with response decoding disabled; normal JSON Redis operations keep
their existing text-decoding connection.

## Generation coherence

Retrieval carries one captured revision set and namespace generation through
snapshot loading, map-nav, reference resolution, and final hydration. A
generation lookup failure is treated as inability to establish coherence and
uses the fallback path. A generation mismatch is never served as a valid
snapshot.

## Diagnostics

Set `RETRIEVAL_SNAPSHOT_TIMING=1` for detailed snapshot decode timings during
local or staging diagnosis. The route CPU metric is process-level CPU time for
the map-nav route. `ru_maxrss` is a process high-water mark, not a request-level
memory peak, and must not be interpreted as one.

