# Demo publication preparation cache

## Purpose

Canonical demo artifacts can be reused across user namespaces. Publishing each
copy still tokenizes the same parsed text and builds the same token-frequency
counts. A bounded process-local cache can reuse those pure results without
changing publication ownership or retrieval semantics.

## Boundary

The cache is enabled only when `DEMO_PUBLICATION_PREPARATION_CACHE_ENABLED=true`
and the canonical bundle supplies a content version. Its scope identifies the
source, content version, map-unit index format, and preparation algorithm
version. Other publication paths continue through the authoritative tokenizers.

Cache values are immutable token sequences or frequency pairs. Callers receive
fresh mutable copies. Document IDs, job result IDs, section IDs, map-unit IDs,
namespace, transaction state, scores, and retrieval results never enter the
cache. Search-field construction and map-unit persistence remain in the shared
publication path and retain the same ordering and transaction boundary.

The process cache is limited to 64 MiB and 4,096 entries. Eviction only causes
recomputation. A changed source content version, index format, or preparation
algorithm does not reuse an earlier entry. The default flag is off, so rollback
is a configuration change and existing persisted documents remain readable.

## Verification

The contract publishes the same canonical demo source in separate namespaces
with the flag off, on with an empty cache, on with a warm cache, and off again.
It compares persisted chunk search fields, map-unit semantics, token-frequency
rows, and IDF statistics after excluding generated database identifiers. It
also checks namespace-scoped retrieval and confirms no document crosses scope.

This verifies semantic parity. Performance admission still requires a measured
publication stage improvement and end-to-end timing on production-shaped input.

## Focused measurements

The catalog's real SpaceX demo (227 chunks) was exercised through the real HTTP
materializer against the small local database, with the canonical bundle
already present. The separate 12.42-million-token clone experiment was not
repeated for this cache:

| Mode | Request 1 | Request 2 | Bundle writes |
| --- | ---: | ---: | --- |
| Preparation cache off | 4.887 s | 5.051 s | 0 new or changed objects |
| Preparation cache on | 7.044 s (cold) | 2.967 s (warm) | 0 new or changed objects |

Both modes used the same canonical ZIP and raw prefix. Page and image assets,
classic retrieval, and archival completed successfully. The warm sample is an
observed local improvement; the cold sample includes cache population and was
slower. These are focused observations, not a production p95 claim.

The final Worker owner check used a small Markdown upload because the local
provider credentials do not establish successful external LLM or PDF parsing.
The Worker completed job `job_f5d5c6d4ed2b` in 3.814 s with a 236 ms publication
trace, and retrieval plus archival passed. The provider returned HTTP 401 and
the parser completed through its fallback; this validates the Worker publication boundary and
fallback behavior, not external model quality.
