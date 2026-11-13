# Production retrieval QPS baseline and first-version planning target

Raw evidence files named in this document (JSON, CSV, SQL, logs and probe copies) are retained privately and are not published in this repository.

Measured on 2026-11-01, America/Los_Angeles. Read-only telemetry and AWS
inspection; no production requests or load tests were generated.

The observed peak arrival rate is **5 retrieval requests in a rolling second**.
The owner's accepted three-times rule therefore gives a first-version
**peak target of 15 user-facing retrieval requests per second**. This is a
planning requirement, not measured OpenSearch capacity or an accepted sustained
15 QPS service-level objective.

## Observation window and request definition

- UTC: 2026-10-19 05:50:00 through 2026-11-02 05:50:00, a 336-hour window.
- User-local time: 2026-10-18 22:50 PDT through 2026-11-01 21:50 PST.
  The daylight-saving transition accounts for the offset change.
- The cutoff was approximately ten minutes before the query began, allowing
  recent requests time to complete and export.
- Project: Logfire `knowhere-api`; service `knowhere-api`; deployment
  environment `production`.
- Count FastAPI server-request spans for POST v1 and v2 retrieval/query,
  including their externally prefixed path forms. Exclude child spans, logs,
  pending-span placeholders, staging, and unrelated endpoints.
- Use request **start timestamps** for arrival QPS. Do not require a null
  parent span, since callers may propagate distributed-tracing parents.
- All 354 returned rows had distinct span IDs. The query limit was 1,000;
  the complete timing set was returned without truncation.

[Open the frozen source view in Logfire](https://logfire-us.pydantic.dev/ontos-ai/knowhere-api/?q=service_name+%3D+%27knowhere-api%27+AND+deployment_environment+%3D+%27production%27+AND+kind+%3D+%27span%27+AND+otel_scope_name+%3D+%27opentelemetry.instrumentation.fastapi%27+AND+span_name+IN+%28%27POST+%2Fv1%2Fretrieval%2Fquery%27%2C+%27POST+%2Fv2%2Fretrieval%2Fquery%27%2C+%27POST+%2Fapi%2Fv1%2Fretrieval%2Fquery%27%2C+%27POST+%2Fapi%2Fv2%2Fretrieval%2Fquery%27%29&since=2026-10-19T05%3A50%3A00Z&until=2026-11-02T05%3A50%3A00Z).

## Observed traffic and scaled reference profile

| Metric | Observed | Three-times reference |
| --- | ---: | ---: |
| Maximum requests in a rolling 1-second window | 5 | 15 |
| Maximum requests in an aligned clock second | 5 | 15 |
| Maximum requests in a rolling 60-second window | 13, or 0.2167 QPS | 39/minute, or 0.65 QPS |
| Maximum requests in an aligned clock minute | 10, or 0.1667 QPS | 30/minute, or 0.5 QPS |
| Maximum requests in a rolling 300-second window | 28, or 0.0933 QPS | 84/5 minutes, or 0.28 QPS |

The primary first-version target is the three-times **peak arrival QPS**:
`5 * 3 = 15`. The longer-window references describe the observed burst shape;
they are not additional latency or sustained-throughput commitments.

There were 354 retrieval requests: 189 on v2 and 165 on v1. HTTP outcomes
were 335 successful responses, 6 HTTP 409 responses, and 13 HTTP 500 responses.
All attempts are included in arrival demand. Operational probes, if present,
are not separated from customer traffic by these aggregates.

One five-request arrival burst occurred on 2026-10-26 at 09:52:52 PDT
(16:52:52 UTC). All five requests succeeded. They started within approximately
209 milliseconds and took 12.18 to 17.42 seconds to complete. The maximum
simultaneous in-flight server requests in the complete observed timing set
was five; that is observed concurrency, not an infrastructure limit.

Rolling windows count request starts in `(t - W, t]` for each request time
`t`, with `W` equal to 1, 60, or 300 seconds. A sorted sliding-window
calculation was independently checked with a direct all-timestamps count.

## Independent access-log reconciliation

The production ECS service uses CloudWatch group `/ecs/knowhere-api-prod`,
which has seven-day retention. Its overlapping window is 2026-10-26 05:50 UTC
through 2026-11-02 05:50 UTC.

| HTTP outcome | CloudWatch access log | Logfire request spans |
| --- | ---: | ---: |
| 200 | 215 | 215 |
| 409 | 6 | 6 |
| 500 | 13 | 13 |
| Total | 234 | 234 |

Access-log matching used the quoted POST retrieval/query HTTP request line,
so request-body log messages were not counted. Counts match exactly across
the two API replicas and both endpoint versions.

CloudWatch's maximum aligned **completion-time** count is three requests in
one second. Bucketing Logfire by request **end time** gives the same maximum.
This is consistent with five requests arriving together and finishing at
different times; it does not contradict the five-request arrival peak.
Using completion timestamps alone would understate this observed arrival
burst.

The inspected repository logging configuration does not set an explicit
sampling rate, and relevant active API task environment variables contain no
sampling override. The seven-day log reconciliation gives independent coverage
evidence for that interval. The preceding seven days of the Logfire window
cannot be independently reconciled against expired CloudWatch logs. These are
observed peaks in the stated window, not an all-time maximum or a guarantee
that every possible request was captured.

## Current production context

AWS read-only inspection identified the production API and worker services
in the existing `knowhere-fargate` cluster:

| Component | Running tasks | CPU per task | Memory per task | Other observed setting |
| --- | ---: | ---: | ---: | --- |
| API, task definition 73 | 2 | 0.5 vCPU | 2 GiB | Production environment; verified production log group |
| Worker, task definition 69 | 2 | 2 vCPU | 4 GiB | `WORKER_CONCURRENCY=10` per task |

Configured worker concurrency is not a completed-job rate. These existing
services do not establish the resources needed by the planned independent
indexing work or its OpenSearch node.

## Capacity implications and next decision

- Keep **15 external retrieval QPS** as the derived first-version peak target.
- Do not translate it directly to OpenSearch HTTP QPS. The planned two lexical
  channels and agent recall fan-out, cache hits, and small-corpus routing change
  internal demand and must be represented in the capacity workload.
- Acceptance preserves the observed production arrival rhythm at three times
  the volume, reaching a 15-QPS peak. Sustained 15 QPS is outside this decision.
- Existing evidence supports mixed-load planning with online retrieval
  protected and background indexing limited/queued. Indexing arrival volume,
  active namespaces, cold-index openings, query mix, and queue drain time
  remain to be established before choosing worker counts or node sizing.
- The proposed 32 GiB single OpenSearch node remains a candidate. No capacity
  acceptance or infrastructure change was performed by this investigation.

The accepted first capacity check preserves the production arrival rhythm,
scales request volume by three, and uses 15 QPS as the peak acceptance target.
Indexing demand and the mixed-load infrastructure configuration remain the
next design topics.

## Retained evidence

- Summary and planning target (`retrieval-qps-2026-11-01/summary.json`).
- Sanitized request timing (`retrieval-qps-2026-11-01/request-timing.csv`).
- Logfire query definitions (`retrieval-qps-2026-11-01/queries.sql`).
- CloudWatch reconciliation and query IDs (`retrieval-qps-2026-11-01/cloudwatch-crosscheck.json`).

Evidence contains timing, route, status, aggregates, and selected infrastructure
settings. Search text, request bodies, customer identifiers, and credentials
are absent.
