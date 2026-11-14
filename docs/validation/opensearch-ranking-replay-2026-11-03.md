# OpenSearch ranking replay on prepared public passages

Date 2026-11-03. The publication replay passed. Reviewed business ranking
acceptance remains pending the 20 business queries and corresponding prepared
corpus revisions and evidence labels.

## Inputs and execution

- Used the four bundled public documents in the local retrieval lab at
  commit `039a7b8b93a02552d8d004123b3110bc348af7ea`. The manifest selected
  the first 20 public cases with positive provisional labels in fixture order,
  substituting `public-21` for the last selected case to retain the known
  provisional ranking probe. All requests used Top 10.
- Republished 1,435 prepared source passages in three namespaces through the
  Knowhere publication code. The first active builds contained 1,422 units
  after publication deduplication. Each replacement reuses the same source
  passages to expose effects from coexisting builds while holding content
  constant. The replay checks section citations for text hits. Different
  replacement content, page-type citations and client API authentication
  need separate coverage.
- Both backends used the classic retrieval entry point through public result
  projection. PostgreSQL used Knowhere’s legacy persisted map units, with a
  baseline for each active revision. The replay bypassed result caching and
  held hit statistics constant.
- Each namespace exercised partial visibility, two further automatic
  executions, exhausted retries, successful manual retry, activation and
  cleanup. Failures were injected immediately after real OpenSearch writes
  became visible. The charged credit count was unchanged after manual retry.
- Oracle ran a dedicated loopback OpenSearch 3.9.0 container with a 640 MiB
  heap, 1,600 MiB container memory cap and 0.7 CPU quota. PostgreSQL used the
  temporary contract-test instance. The comparison chat’s service was outside
  this run. No paid APIs, cloud storage or additional instances were used.

## Results

Pytest reported one pass in 121.95 seconds. All 240 query-stage observations
checked the active publication and all 4,560 returned rows matched their
document, revision, chunk and section. The OpenSearch index still contained
the partial and failed builds during the relevant checks. Only active builds
remained after ordinary cleanup.

| Stage | PG evidence in Top 10 | OpenSearch evidence in Top 10 | OpenSearch Top 10 list changed |
| --- | ---: | ---: | ---: |
| before-staging | 19/20 | 19/20 | 0/20 |
| parsed-unactivated | 19/20 | 19/20 | 0/20 |
| partial-visible | 19/20 | 19/20 | 9/20 |
| after-failure-1 | 19/20 | 19/20 | 9/20 |
| automatic-retry-visible | 19/20 | 19/20 | 9/20 |
| after-failure-2 | 19/20 | 19/20 | 9/20 |
| complete-unactivated | 19/20 | 19/20 | 8/20 |
| after-failure-3 | 19/20 | 19/20 | 8/20 |
| explicit-retry-visible | 19/20 | 19/20 | 6/20 |
| activated-before-cleanup | 19/20 | 19/20 | 0/20 |
| cleanup-in-progress | 19/20 | 19/20 | 0/20 |
| after-cleanup | 19/20 | 19/20 | 0/20 |

List changes include both order and membership. The reference is the clean
old build until activation and the new build immediately after activation
thereafter. The earliest matching evidence rank stayed constant for each
query within each backend across all stages. Activation itself changed the
OpenSearch Top 10 list for 6 queries despite identical source passages.
PostgreSQL and OpenSearch had different lists on 17 queries before staging
and 15 after cleanup.

The common miss was `encoder decoder`. The provisional evidence points to
Transformers tutorial pages 47 and 52. Both backends returned passages from
pages 21 and 32. This case remains a shared miss against those labels.

## Per-query first evidence rank

| Case | Query | PostgreSQL | OpenSearch |
| --- | --- | ---: | ---: |
| public-00 | data center revenue | 1 | 1 |
| public-01 | HBM revenue | 2 | 1 |
| public-02 | gross margin | 1 | 1 |
| public-03 | operating cash flow | 1 | 1 |
| public-04 | capital expenditures | 1 | 1 |
| public-05 | fiscal 2026 outlook | 1 | 1 |
| public-06 | GAAP diluted earnings per share | 1 | 1 |
| public-07 | DRAM NAND | 1 | 1 |
| public-08 | scaled dot product attention | 3 | 3 |
| public-10 | multi head attention | 1 | 1 |
| public-11 | encoder decoder | Miss | Miss |
| public-12 | self attention | 1 | 1 |
| public-13 | query key value | 1 | 1 |
| public-14 | masked language model | 1 | 1 |
| public-15 | Starlink subscribers | 1 | 1 |
| public-16 | Falcon reuse launch | 1 | 1 |
| public-17 | Starship development | 1 | 1 |
| public-18 | risk factors | 1 | 1 |
| public-19 | related party transactions | 1 | 1 |
| public-21 | share voting | 1 | 1 |

## Reproduction

Run `test_opensearch_ranking_replay.py` against a dedicated loopback node
with `KNOWHERE_TEST_OPENSEARCH_URL`, `KNOWHERE_RANKING_MANIFEST` and
`KNOWHERE_RANKING_OUTPUT` set. The manifest defines each prepared document
and optional replacement plus 20 queries, their namespaces, Top K and
required evidence groups. Each group can list interchangeable chunk IDs.
Use `business_reviewed` for the reviewed set. A missing required OpenSearch
evidence group then fails the test.

The replay manifest and raw ranks, source identities, scores and physical
build counts are in `ranking-preview-a9f4` under the isolated Oracle checkout.
The temporary OpenSearch container and its data were removed.

- Manifest SHA-256 `a0bc0edb7c608ae78a147687d105f66cc870ebaf12564653ca694a240f0e29f1`
- Result SHA-256 `bde27dc4f3fe3d50b4d8f7f1ff3fac71aa77e0b2c23978b614bc90221b78eb3d`
