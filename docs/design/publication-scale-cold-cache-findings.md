# Publication at production scale with a cold database cache

## Scope and environment

This investigation follows the runtime repair in PR #448. We ran the actual
API application and Celery Worker against a disposable PostgreSQL clone on the
Mac benchmark host. Redis and S3 were isolated local services. Production was
inspected read-only; no production configuration, data, or indexes were changed.

The clone was copied from a cleanly stopped template mounted read-only. We
applied current migrations and restored the two missing token indexes before
comparison. Historical catalog evidence from 2026-09-30 02:07:35 UTC confirms
that the five-index configuration also existed before the production failure.

| Property | Clone configuration |
| --- | --- |
| Token rows, catalog estimate | 12,423,317 |
| Token heap | 2,385,043,456 bytes |
| Initial total token relation including indexes | 8,440,152,064 bytes |
| PostgreSQL | 15.19 |
| Container memory / swap limit | 1 GiB / no additional swap |
| CPU limit | One CPU |
| Shared buffers | 128 MiB |
| Work memory / maintenance work memory | 4 MiB / 64 MiB |
| Effective cache size | 512 MiB, planner estimate only |
| Maximum connections | 40 |
| Durability | fsync, full_page_writes, synchronous_commit enabled |
| I/O timing | Enabled |
| API statement / command timeout | 30 seconds / 30 seconds |

Before each publication sample, we stopped only the disposable database, used
`POSIX_FADV_DONTNEED` on its data files, and restarted it. This clears PostgreSQL
buffers and requests eviction of the clone's Linux file cache. We did not clear
global host caches or restart the sealed source. macOS/Colima virtual-disk caches
remain uncontrolled. The API and Worker ran on the development machine through
an SSH database tunnel, so application timing includes network round trips.

Each materialization used the same `demo-spacex-s1` input: 227 chunks and 10,778
token rows. We captured `pg_stat_statements`, per-index read counters, publication
traces, and sampled wait events. Samples used separate namespaces; successfully
published documents were retrieved and archived. The same clone was reused,
with cache eviction between samples; earlier successful samples leave a small
amount of additional archived data. These are targeted observations, not
statistical latency percentiles or identical snapshots.

## Batch repair comparison

Both variants used candidate publication and the same five token indexes:
primary key, `lookup`, `token_lookup_binary`, `unit`, and `unit_lookup`.

| Measurement | Before PR #448 | After PR #448 |
| --- | ---: | ---: |
| Complete HTTP materialization | 50.943 s | 54.149 s |
| Publication transaction | 10.416 s | 11.376 s |
| Token persistence stage | 3.652 s | 4.228 s |
| Token COPY calls | 1 | 11 |
| Longest token command, application measurement | 3.551 s | 0.482 s |
| Token COPY execution, database measurement | 3.552 s | 3.848 s |
| Token COPY buffer reads | 25,639 | 25,673 |
| Token COPY read time | 3.092 s | 3.148 s |
| Token COPY WAL bytes | 159,152,618 | 160,566,253 |

Both requests succeeded with classic retrieval and archive. Neither reproduced
the production 30-second timeout. The first sample attributed 12 active samples
to token COPY waiting on `DataFileRead`, consistent with the database timing.

The batching change bounded individual command duration but did not reduce
storage reads. In this pair, token-stage duration increased approximately 16%
and publication duration approximately 9%. Database execution increased by
approximately 0.30 seconds; additional connection round trips contribute to
the larger application-observed difference. A single pair cannot establish a
stable regression percentage or a production throughput estimate.

This also corrects the interpretation of the earlier small-database run:
publication below ten seconds did not establish that latency with a large,
cold database. Full HTTP time includes preparation outside publication.

## Attribution and a focused index experiment

The first publication produced these per-index buffer-read deltas:

| Token index | Read blocks |
| --- | ---: |
| Primary key | 148 |
| Legacy `lookup` | 13,435 |
| `token_lookup_binary` | 11,015 |
| `unit` | 400 |
| `unit_lookup` | 1,416 |

These counters cover the publication observation window and are separate from
COPY's statement-level accounting. Both token-leading indexes dominate reads;
legacy `lookup` accounts for approximately half of observed index reads.

Current classic discovery and `corpus.recall` use the binary token hash
predicate. Oversized outline/node filtering still has a normal-hash reader in
`scoring/map_lighting.py`, constrained by map-unit IDs and channel. The retained
`unit_lookup` covering index supports that reader.

Ten read-only production EXPLAIN plans, spanning scopes of 1, 32, 227, 1,000,
and 2,000 map units and two token sets, selected `unit_lookup`. On the clone,
we froze equivalent fixtures and compared exact result counts and SHA-256
digests before and after removing only the nonunique legacy `lookup` index.
All ten cases matched; every plan continued to use `unit_lookup`. These checks
do not exercise every historical application query or a full agentic episode.

### Cold publication after removing legacy lookup

The final sample retained the patched application and all other settings, and
removed only `idx_document_map_unit_tokens_lookup` in the disposable clone.

| Measurement | Patched, five indexes | Patched, legacy lookup removed |
| --- | ---: | ---: |
| Complete HTTP materialization | 54.149 s | 54.177 s |
| Publication transaction | 11.376 s | 10.182 s |
| Token persistence stage | 4.228 s | 2.624 s |
| Token COPY execution, database measurement | 3.848 s | 1.989 s |
| Longest token command, application measurement | 0.482 s | 0.290 s |
| Token COPY buffer reads | 25,673 | 12,110 |
| Token COPY read time | 3.148 s | 1.618 s |
| Token COPY WAL bytes | 160,566,253 | 85,682,583 |

Publication, chunk reads, classic retrieval, and archive all succeeded. Token
buffer reads fell approximately 53%, WAL approximately 47%, database COPY time
approximately 48%, and application token-stage duration approximately 38%.
Publication improved approximately 10.5% in this comparison, while full HTTP
latency remained essentially unchanged. Preparation and other stages still
matter, and the publication transaction remains slightly above ten seconds.

This isolates a concrete write-cost reduction. It supports preparing legacy
lookup retirement as a separate index change while retaining binary lookup,
unit lookup, the unit index, and the primary key. It does not justify removing
binary lookup, changing retrieval semantics, or claiming production latency.
Before a production index change, account for application versions supported
by rollback and the time/storage required to rebuild the old index. The local
query checks do not cover every scope size or historical query shape.

## Worker verification

An HTTP-created Markdown ingestion job traversed real upload, confirmation,
Redis/Celery dispatch, gevent Worker startup, parsing, and publication against
the full five-index clone. It completed in 7.707 seconds end to end; Worker
processing reported 6.519 seconds. Both chunks were readable, classic retrieval
returned the document, and archive succeeded. This checks Worker compatibility
against the large database; the small input is not a full-PDF throughput test.
Copied job callback URLs were cleared in the disposable clone before Worker
startup to isolate external delivery.

## Limits and evidence

Production's observed token relation occupied approximately 15.54 GB after
recovery, versus 8.44 GB in the rebuilt clone. Row counts are comparable, but
index density, physical size, churn, and storage behavior are different.
The clone's first token COPY averaged approximately 0.121 ms per PostgreSQL
buffer read. Historical Aurora read latency was approximately 0.8 ms at one-minute
resolution; these are different measurement layers and cannot be substituted
into a precise production latency prediction. Aurora scale-up is not simulated.

Local artifacts are under `/tmp/knowhere-publication-scale/`: before/after HTTP
results, database snapshots, trace summaries, `comparison.json`, index alignment
DDL, historical inventory evidence, and map-lighting parity results. Environment,
authentication, and frozen data fixtures are private local artifacts and are not
included in this document. No production deployment or index retirement is
performed by this investigation.

## Experiment completion

Only three large-document publication samples were run: old candidate batching,
patched batching, and patched batching without legacy lookup. There was one
small real Worker job and ten narrowly scoped query parity checks. No broad
benchmark suite or full PDF reparse was started. The disposable database is
retained stopped with the four-index experimental configuration for follow-up;
the source/template are unchanged. Runtime processes and the measurement tunnel
were stopped after capture.
