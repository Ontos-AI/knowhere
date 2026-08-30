# Use coherent retrieval-serving generations

- Status: Accepted
- Context: A namespace can contain many active document revisions, and publication can replace them while a retrieval request is loading serving metadata and scoring statistics.
- Decision: Assign each namespace a serving generation. Retrieval captures one generation and verifies it across serving reads; if it changes, retry once and use the exact legacy path if consistency cannot be established.
- Consequences: Retrieval never combines incompatible revision metadata and scoring statistics. Publication and retrieval need a small amount of generation bookkeeping, and rare concurrent updates may cause a retry or slower fallback.
