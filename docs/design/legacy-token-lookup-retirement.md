# Legacy token lookup retirement

## Purpose and evidence

Retire only the nonunique `idx_document_map_unit_tokens_lookup` index to reduce
token persistence I/O. Keep the primary key, binary token lookup, unit index,
and unit-leading covering lookup.

The production incident's token COPY samples waited on `IO:DataFileRead`.
In the disposable 12.42-million-row clone, removing this legacy index reduced
COPY buffer reads by approximately 53%, WAL by 47%, database COPY execution time
by 48%, and application token-stage time by 38%. Publication improved from
11.376 to 10.182 seconds in that pair; full HTTP time stayed around 54 seconds.
These are three targeted cold-cache publication observations, not production
latency guarantees. See [the experiment report](publication-scale-cold-cache-findings.md).

Current classic discovery and recall use binary lookup. The remaining plain
token-hash reader in `scoring/map_lighting.py` also scopes by map-unit IDs and
channel, which the retained unit-leading index covers. Ten frozen query cases
returned identical counts and hashes before and after retirement; each used
the retained unit lookup. Historical application versions and every possible
query shape have not been validated.

## Deployment behavior

Revision `3c4d5e6f7a8b`, following `2b3c4d5e6f7a`, retains the legacy index by
default. No runtime application flag deletes or recreates this index. The
previous retirement migration `2f3g4h5i6j7` is unconditional and concerns two
different indexes; this change does not alter that historical migration.

Before the new revision is recorded, an explicit migration invocation can opt in:

```bash
cd apps/api
uv run alembic -x retire_legacy_token_lookup=true upgrade head
```

An ordinary deployment records the revision without retirement. Re-running
`upgrade head` with that argument later cannot execute an already-recorded
revision. Use the operator command below to activate at the same head, including
after later migrations. Do not downgrade the application schema for activation.

## Operator procedure

Supply `DATABASE_URL` through the protected environment. The command also honors
`DB_SSL_MODE`, `DB_SSL_CERT`, `DB_SSL_KEY`, and `DB_SSL_ROOT_CERT`; absent explicit
values, connection URL and libpq defaults apply. Do not put credentials in shell
arguments or reports. Run commands from the repository root with workspace
dependencies installed.

1. Confirm the intended database and the application versions needed for rollback.
   For production, obtain explicit approval for this index change before applying
   it. The investigation and preparation do not authorize a production mutation.
2. Inspect the catalog without changing indexes:

   ```bash
   uv run --package knowhere-api-app python scripts/retire_legacy_token_lookup.py
   ```

3. Explicitly retire the legacy lookup:

   ```bash
   uv run --package knowhere-api-app python scripts/retire_legacy_token_lookup.py --apply
   ```

4. Review the returned catalog. The legacy lookup must be absent; the primary key,
   binary lookup, unit index, and unit lookup must remain valid and ready. Check a
   real publication and retrieval before deciding to retain the change. Observe
   token persistence timing and database I/O; an HTTP success alone does not
   establish an improvement.

The helper verifies the binary and unit-leading lookup definitions and their
`indisvalid` and `indisready` states before removing anything. It also validates
the legacy definition before deletion. A same-name partial, incompatible, or
invalid index fails closed. Repeating apply is safe when the legacy index is
already absent and both replacements remain compatible. Only the named legacy
index is targeted; no table data or uniqueness constraint is removed.

Run one maintenance operation at a time and coordinate other schema changes.
Independent migration and operator connections use concurrent DDL. The operator
sets a five-second lock timeout and a 30-minute statement timeout. Adjust the
latter with `--statement-timeout-seconds` if the rebuild needs a longer window.
Concurrent DDL can still wait for transactions and consume substantial I/O.
Externally owned migration transactions use regular DDL for compatibility with
contract fixtures; do not wrap production index maintenance in such a transaction.

## Rollback

Restore the exact old nonunique index independently of the Alembic head:

```bash
uv run --package knowhere-api-app python scripts/retire_legacy_token_lookup.py --restore
```

This creates concurrently:

```sql
CREATE INDEX idx_document_map_unit_tokens_lookup
ON public.document_map_unit_tokens USING btree (channel, token_hash, map_unit_id);
```

The command reads back and verifies the definition, validity, and readiness.
Repeating restore with a compatible valid index is a no-op. Downgrading the new
revision also restores the index, but is unnecessary for this operator rollback.
Application rollback alone does not restore a dropped index.

Rebuilding scans the token table and consumes storage, CPU, I/O, and WAL; it may
take substantially longer than dropping the index. Allow space for the index and
temporary build work, and account for replica lag. If a concurrent operation is
interrupted, inspect the catalog. An invalid same-name index is deliberately
rejected, not silently reused or automatically deleted. Resolve that catalog
state as a separately reviewed operator action, then retry restoration. Until
the rebuild succeeds, the previous index configuration has not been restored.

## Verification boundary

Disposable PostgreSQL contracts cover default retention, opt-in migration,
independent and externally owned transactions, operator activation after the
revision is already recorded, idempotent apply/restore, exact restoration, and
refusal when replacement indexes are missing, incompatible, invalid, or not
ready. They also reject an incompatible legacy index. No production index is
changed by these tests or by adding this revision.
