# Use a token-leading covering index for map-unit lookup

**Status: accepted.** Token-selective retrieval will use an additive,
idempotent PostgreSQL covering index led by `(channel, token_hash, map_unit_id)`
and including `(token, frequency)`. This preserves the token-index-driven query
shape and allows index-only plans where PostgreSQL visibility permits them. The
migration is additive and keeps the existing lookup index until a separate
production-plan and load review proves it redundant. Requests fall back to the
exact legacy reader when the index or serving data is missing or incomplete.
The additional storage and write-maintenance cost is accepted in exchange for
lower first-request retrieval latency and row transfer.
