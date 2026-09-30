# Publication runtime failure diagnosis

Evidence collected on 2026-09-30. Production inspection was read-only. Local
publication used a disposable PostgreSQL 15.17 database, Redis, LocalStack, and
the real API and Worker entry points.

## Production materialization timeout: confirmed cause

The failed SpaceX demo request started at `2026-09-30T03:07:58.245481Z` and
raised `TimeoutError` at `03:09:16.328Z`. The production API log stream was
`api/api/88eebaaa69a4479e811bbd3fd0a62d95` in `/ecs/knowhere-api-prod`.

Performance Insights for Aurora instance `knowhere-database-prod-instance-1`
provides direct attribution:

- The statement was `COPY document_map_unit_tokens (...) FROM STDIN (FORMAT binary)`.
- It appeared in 30 consecutive one-second samples, from `03:08:47Z` through
  `03:09:16Z`.
- Filtering by that SQL identifier, all 30 samples were `IO:DataFileRead`.
- No lock wait or CPU sample was attributed to this statement. Total sampled
  database load in the surrounding interval never exceeded one active session.

The immediate cause was waiting for database pages to be read from storage until
COPY exhausted the API's 30-second command budget. This establishes storage read
pressure, rather than an application deadlock or slow Python serialization, as
the dominant observed wait. Sampling does not identify the exact relation or
index supplying each read, and it does not prove that no shorter other waits
occurred between samples.

CloudWatch provides supporting context at one-minute resolution:

| Metric | 03:07 UTC | 03:08 UTC | 03:09 UTC |
| --- | ---: | ---: | ---: |
| Serverless capacity, ACU | 0.5 | 0.742 | 2.0 |
| Buffer cache hit ratio | 100% | 99.88% | 88.93% |
| Read IOPS | 0 | 39.2 | 413.7 |
| Read latency | 0 | 0.885 ms | 0.817 ms |
| CPU utilization | 23.9% | 39.9% | 40.0% |

These observations support a cold working set and storage reads during scale-up.
They do not isolate scale-up as the sole cause. Current configuration reports
0–4 ACU and a 300-second auto-pause interval; it must not be substituted for
historical configuration. Historical ACU utilization reached 100% at 2 ACU.

## Current production token storage

A subsequent read-only catalog query estimated 13,276,691 token rows. The heap
occupied 3,394,764,800 bytes and total relation storage was 15,540,740,096 bytes.
All five indexes were valid and ready:

| Index | Leading keys | Bytes |
| --- | --- | ---: |
| `document_map_unit_tokens_pkey` | `id` | 1,468,669,952 |
| `idx_document_map_unit_tokens_lookup` | `channel, token_hash, map_unit_id` | 4,612,505,600 |
| `idx_document_map_unit_tokens_token_lookup_binary` | `channel, decode(token_hash)` | 1,591,910,400 |
| `idx_document_map_unit_tokens_unit` | `map_unit_id, channel` | 285,138,944 |
| `idx_document_map_unit_tokens_unit_lookup` | `map_unit_id, channel, token_hash` | 4,184,621,056 |

The binary and unit-lookup indexes include token/frequency payload. The total
index footprint is approximately 12.14 decimal GB. Index maintenance is a
plausible source of the observed reads; Performance Insights alone does not
identify which index dominated. These sizes are not a measured bloat estimate.
This catalog snapshot was taken after production recovery; it does not establish the index inventory or sizes
at the time of the failed candidate request.

## Real local API result

An HTTP request to the actual `main.py` API materialized `demo-spacex-s1` with
`candidate` enabled and the unchanged 30-second database timeouts. It completed
with HTTP 200 in 46.289 seconds. The document was then archived and checked.

The resulting input and index contained 227 chunks, 227 map units, and 10,778
token rows. The demo input consists of one 1,467,990-character page chunk, 94 image
chunks, and 132 table chunks. It differs from the older 922-chunk benchmark input.

A separate read-only observer sampled the database every 250 ms:

- The publication transaction lasted approximately 8.6 seconds.
- Token COPY appeared in two samples spanning approximately 0.3 seconds and
  finished in under one second. Samples included `DataFileRead` and `WALWrite`.
- Chunk COPY lasted approximately 1.4 seconds.
- No blocking PID was observed.
- Much of the request time preceded the publication transaction.

The production timeout did **not** recur against this almost-empty local
relation. This successful result does not establish performance against the
production token/index footprint.

## Validation gaps and repair choices

The old benchmark created standalone SQLAlchemy engines with `NullPool`, without
the API's 30-second `statement_timeout` and asyncpg `command_timeout`. Its sync
owner also bypassed Worker startup and the gevent/psycogreen callback. Those gaps
explain why it could not establish deployment compatibility.

Before this repair, the candidate wrote up to 100,000 token rows in one COPY
command. Baseline uses 5,000-row INSERT batches. This changes the amount of work governed by one
statement timeout even when both paths share the same enclosing transaction.

Worker compatibility and bounded COPY are implemented and verified below.
Remaining performance investigation:

1. Compare that change against current batching on a disposable large clone,
   using the actual API, production index definitions, the same SpaceX demo,
   30-second timeouts, and database wait sampling. Use a separate cold clone per
   variant rather than running one after another against warmed indexes.
2. Attribute index read cost on that clone before changing index inventory or
   row ordering. Existing sorting by `channel, token_hash, map_unit_id` already
   favors both token-leading indexes. Sorting by map unit would favor the
   unit-leading indexes while sacrificing that locality; it has no demonstrated
   net benefit yet.

## Local evidence files

The following sanitized artifacts were retained in
`/tmp/knowhere-publication-repro/`:

- `demo-before.log`: actual HTTP result and archive completion.
- `demo-before-db-summary.json`: local database samples for that request.
- `database-observation.jsonl`: captured local wait/progress samples.
- `prod-failure-pi-waits.json`: surrounding database wait samples.
- `prod-failure-pi-sql.json`: SQL attribution in the same interval.
- `prod-failure-token-copy-waits.json`: token COPY filtered wait samples.
- `prod-failure-cloudwatch.json`: capacity, cache, CPU, and I/O metrics.
- `prod-current-token-indexes.json`: current relation sizes and index definitions.

Production evidence was collected through read-only inspection. No production
configuration or data was changed.

## Implemented repair and verification

Worker startup installs `psycogreen`'s process-wide wait callback. Psycopg2
[does not support COPY with a registered wait callback](https://www.psycopg.org/docs/advanced.html#support-for-coroutine-libraries).
Both chunk and token persistence now use bounded `execute_values` INSERTs on the
owning connection when that callback is installed. Compatible psycopg2 and
asyncpg connections retain COPY. The repair never disables the global callback,
opens another connection, or commits independently. Worker INSERT fallback does
not retain all the performance characteristics of native COPY.

Both persistence helpers now bound each statement to 1,000 rows. Token splitting
applies within a single large map unit as well as between map units. Chunk SQL
tracing records each actual batch separately. This limits work covered by each
command timeout while retaining a single publication transaction. Neither the
API's 30-second command timeout nor its 30-second statement timeout was raised.
A sufficiently slow batch can still time out; this is not an absolute latency
or availability guarantee.

The actual local Worker, launched through `apps/worker/worker.py`, first failed
an HTTP-created Markdown ingestion job with the production error:
`copy_expert cannot be used with an asynchronous callback`. After the callback
compatibility fix, the same HTTP/upload/confirm-upload/Celery flow completed in
4.282 seconds, with a 397.991 ms publication transaction. Its two chunks were
visible through HTTP and the document was then archived. This small ingestion
checks Worker runtime compatibility, not full PDF parser throughput.

Focused database contracts cover native psycopg2, the actual psycogreen callback,
asyncpg, escaped text/JSON/NULL values, callback preservation, and rollback. A
slow-write contract deliberately delays PostgreSQL token writes proportionally
to inserted rows and retains a short per-command budget. The original batching
failed with `TimeoutError`; bounded batches completed and rolling back the owner
transaction removed all 2,501 rows. This is controlled fault injection, not a
simulation of Aurora's exact storage/cache behavior.

### Real API timeout fault injection

A temporary statement trigger in the isolated local database adds 3.5 ms of
wait per inserted token. It uses the statement's transition table to calculate
one server-side delay, without changing application code or database timeouts.
The real SpaceX API request under the original batching returned HTTP 500 after
64.453 seconds and logged `TimeoutError` in `asyncpg.copy_records_to_table`.
With bounded batches and exactly the same injected wait, the request returned
HTTP 200 after 75.295 seconds. Publication took 42.434 seconds, including 38.191
seconds of token persistence split across 11 commands; the longest token command
was 3.543 seconds. The document contained all 227 chunks and was archived. The
failed request left zero document, section, and chunk rows in its namespace.

This proves that the change avoids the reproduced per-command timeout without
relaxing the timeout or splitting the transaction. It does not show faster disk
I/O: total artificial delay was intentionally unchanged. The delay trigger and
function were removed from the disposable database after this run. Production
was not modified.

### Final run without injected delay

The final code was loaded by restarting both real services. SpaceX demo
materialization returned HTTP 200 in 38.236 seconds, including a 5.646-second
publication transaction and 824 ms token persistence (11 token commands,
maximum SQL duration 91 ms). The 227 chunks were readable, classic retrieval
returned the published document, and archive succeeded. This single small,
previously exercised local database is not a cold production-scale comparison.
The HTTP duration also includes bundle preparation outside the publication
transaction; it remains above ten seconds.

A concurrent HTTP-created Worker job completed successfully (worker-reported
processing duration 4.932 seconds; publication 247 ms); retrieval and archive
also passed. Its client flow took 37.479 seconds while the API was simultaneously
materializing the demo, so that wall time is not an isolated Worker throughput
measurement. Agentic retrieval and a new full PDF parse were not exercised.

All seven focused contracts passed. Ruff, targeted Pyright, and `git diff
--check` passed. The API and Worker used isolated local services. No production rollout was
performed.

Additional reproduction artifacts in `/tmp/knowhere-publication-repro/`:
`repro-http.py`, `slow-token-writes.py`, `copy-red.log`, `timeout-red.log`,
`demo-timeout-red.log`, `demo-timeout-green.log`, `demo-final.log`,
`worker-final-result.log`, and `final-contracts.log`. The temporary slowdown
script rejects non-local database endpoints. Environment and authentication
files in that directory contain local credentials and must not be shared.

Re-run the focused contracts from `apps/api`:

```sh
../../.venv/bin/python -m pytest \
  tests/contract/test_publication_chunk_copy_contract.py \
  tests/contract/test_publication_token_copy_contract.py -q --tb=short
```
