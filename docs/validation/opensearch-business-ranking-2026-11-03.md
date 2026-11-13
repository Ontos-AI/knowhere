# OpenSearch ranking replay on logged business queries

Date 2026-11-03. Version, body, citation, retry and cleanup assertions passed.
The ranking gate failed on seven shared misses against the frozen evidence labels.

## Source and sample

- Queried existing production Logfire records from October 22 00:00 UTC to
  November 3 19:00 UTC. The owner and namespace of 143 successful requests
  matched the Oracle frozen scope U2. These requests contained 136 distinct
  query texts. The selected 20 ran between October 26 16:37 UTC and October 27
  02:45 UTC. All 20 completed through classic Top K with Top K set to 10 and
  empty document and section exclusions.
- The 20 query strings cover four related guideline lookup intents, with
  multiple Chinese and English formulations. They are requests observed in
  production. This selection excluded individual case histories and includes
  no claim that all formulations came from distinct users or sessions.
- Matched frozen document and revision IDs against the local copy of the
  backup that produced the Oracle fixture. The backup SHA-256 matched. Used
  18 complete prepared documents from that namespace, with 2,633 source
  chunks and 1,727 active lexical units. The original frozen scope contains
  644 documents. Both engines used the same 18-document subset and defaults
  for options absent from the request logs. The logs omit the request body
  and do not establish whether an include filter or nondefault options were
  sent. This comparison qualifies the sampled lexical workload only.
- Reviewed 14 evidence anchors directly in the prepared passages before
  scoring. The private manifest binds each trace to required evidence
  groups by source document, revision and chunk ID. A query passes when
  every group has at least one returned passage within Top 10.
- Replayed a controlled replacement of one document ID using two different
  prepared pulmonary embolism guidelines from the same backup. The 2019
  guideline contributed 151 source chunks and 90 lexical units. The 2026
  guideline contributed 184 source chunks and 120 lexical units. The new
  edition was held out of the old scope and its evidence labels were frozen
  separately. This fixture pairs two original source files under one test
  document ID. It does not represent an observed production version history.

## Results

The actual classic retrieval path and the PostgreSQL map-unit baseline ran
against the same eligible revision set. Across 240 query-stage observations
and 4,800 results, every returned document, revision, section and source
filename matched the published data. Text result bodies matched their bound
rows. OpenSearch returned 13 distinct chunks from the replaced document
before activation and 10 distinct chunks after activation. The sets were
disjoint. Automatic retries exhausted, manual retry activated the replacement
without changing charged credits, and cleanup removed the retired and three
failed builds. Active builds returned to 18 after cleanup.

| Stage | PG evidence hit | OpenSearch evidence hit | OpenSearch Top 10 lists changed | Physical builds |
| --- | ---: | ---: | ---: | ---: |
| before-staging | 13/20 | 13/20 | 0/20 | 18 |
| parsed-unactivated | 13/20 | 13/20 | 0/20 | 18 |
| partial-visible | 13/20 | 13/20 | 5/20 | 19 |
| after-failure-1 | 13/20 | 13/20 | 5/20 | 19 |
| automatic-retry-visible | 13/20 | 13/20 | 10/20 | 20 |
| after-failure-2 | 13/20 | 13/20 | 10/20 | 20 |
| complete-unactivated | 13/20 | 13/20 | 13/20 | 21 |
| after-failure-3 | 13/20 | 13/20 | 13/20 | 21 |
| explicit-retry-visible | 13/20 | 13/20 | 13/20 | 22 |
| activated-before-cleanup | 13/20 | 13/20 | 0/20 | 22 |
| cleanup-in-progress | 13/20 | 13/20 | 0/20 | 21 |
| after-cleanup | 13/20 | 13/20 | 0/20 | 18 |

List changes include order and membership. The reference is the clean old
build before activation and the newly active build immediately before
cleanup. The first matching evidence rank was stable while the old version
served, and remained stable throughout cleanup of the new active version.
The two engines returned different ordered Top 10 lists for 18/20 queries
both before staging and after cleanup. OpenSearch ranked the same required
evidence earlier on Q010, Q011 and Q014. No PG anchor hit became an
OpenSearch miss.

The seven fixed-label misses were all English variants. Q006 returned an
ECG diagnostic section first in both engines, which is useful partial
evidence outside the frozen diagnostic anchors. Q012 and Q013 retrieved
relevant sections of the old guideline outside the anchors. After activation,
both returned a references section first and four to five references
sections in Top 10. Neither returned the marked recommendation passage.
Q017 and Q018 returned bleeding-management and references sections, which
do not establish the requested drug-selection evidence. The fixed labels
were kept unchanged for the scored gate.

## Per-query first matching evidence rank

Two numbers in a cell show two required evidence groups. Dashes are misses.

| Case | Intent | Old PG | Old OpenSearch | New PG | New OpenSearch |
| --- | --- | ---: | ---: | ---: | ---: |
| Q001 | INR | 7 | 7 | 7 | 7 |
| Q002 | INR | — | — | — | — |
| Q003 | INR | 4 | 4 | 3 | 3 |
| Q004 | INR | 1 | 1 | 1 | 1 |
| Q005 | NSTEMI diagnosis | 1 | 1 | 1 | 1 |
| Q006 | NSTEMI diagnosis | — | — | — | — |
| Q007 | NSTEMI diagnosis | 1 | 1 | 1 | 1 |
| Q008 | NSTEMI diagnosis | 2 | 2 | 2 | 2 |
| Q009 | NSTEMI diagnosis | — | — | — | — |
| Q010 | PE thrombolysis | 6 | 5 | 6 | 5 |
| Q011 | PE thrombolysis | 6 | 5 | 6 | 5 |
| Q012 | PE thrombolysis | — | — | — | — |
| Q013 | PE thrombolysis | — | — | — | — |
| Q014 | PE thrombolysis | 4, 4 | 3, 3 | 4, 4 | 3, 3 |
| Q015 | PE thrombolysis | 6 | 6 | 6 | 6 |
| Q016 | AF anticoagulant selection | 3 | 3 | 3 | 3 |
| Q017 | AF anticoagulant selection | — | — | — | — |
| Q018 | AF anticoagulant selection | — | — | — | — |
| Q019 | AF anticoagulant selection | 1 | 1 | 1 | 1 |
| Q020 | AF anticoagulant selection | 2 | 2 | 2 | 2 |

## Isolation and evidence

Oracle used a dedicated loopback OpenSearch 3.9.0 container and a temporary
PostgreSQL contract instance. The OpenSearch container was limited to 0.7 CPU
and 1,600 MiB of memory with a 640 MiB JVM heap. The run used the cached image,
prebuilt data, local storage and no model calls. The comparison chat’s service
was outside this run. The temporary container and database were cleaned up.

Pytest completed in 154.16 seconds and failed on the final frozen-evidence
assertion. Private fixture files, the evidence mapping, raw results and the
pytest log are in the isolated Oracle checkout under
`ranking-business-c7e5`. Raw production query text and identifiers were
kept out of this report and the repository.

Handoff clarification (2026-11-04): the current `business_reviewed` test fails
if any OpenSearch evidence rank is missing, even when PG also misses it. This
recorded run is therefore a known failing test, while its relative result
preserves all 13 PG-found queries and lists seven shared misses. It must not
be relabeled as passing. The accepted replacement gate requires retaining
PG-found evidence and independently correct permissions/body/version/citations;
shared misses are reported separately. Correct the evaluator and rerun the
unchanged 20-query baseline plus a separately versioned extended suite under
[development-plan Section 8.1 and KW-09](../design/opensearch-development-plan.md#81-align-the-regression-test-with-the-accepted-quality-gate).
The current exactly-20 manifest schema does not yet support that extension.

- Backup SHA-256 `d6a4f13e1cd8712719333d8701648a6eebe6d387090b1ce1422a2f1135565f7f`
- Manifest SHA-256 `445f541447028a07ac39b82b4df578384fdfdfcde48a88f5bd541b2e98f4c421`
- Result SHA-256 `506def90edd7631f807e94470c45c0fcdfe3e0786790191e31701c2fc764271c`
