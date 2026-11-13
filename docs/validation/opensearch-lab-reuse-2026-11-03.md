# Reuse of the existing Oracle retrieval lab

Raw evidence files named in this document (JSON, CSV, SQL, logs and probe copies) are retained privately and are not published in this repository.

Date: 2026-11-03. Status: existing service verified with bounded read-only
comparisons. This is a reuse/reproducibility check, not publication, ranking
transition, production capacity, or disaster-recovery acceptance.

The owner supplied [the running lab](http://100.77.144.81:18765/) on
the Oracle lab host. HTTP and SSH access succeeded. Reuse this deployment and
its frozen fixture instead of creating another comparison environment. No
application source, remote configuration, containers, index contents/state,
or benchmark workloads were changed in this check. The Mac only acted as
an HTTP/SSH client; the native search backend was the existing Oracle node.

## Verified service and inputs

- `/api/health` returned `ready` before and after the comparisons.
- Manifest: OpenSearch **3.9.0**, Lucene **10.5.1**, build
  `4ee42a94e87f66fbf1e62a9871b1b87f91e02472`.
- Corpus: **2,955 units, 66 logical documents, six namespaces, 48 queries**;
  46 queries have provisional positive evidence annotations. It contains
  1,435 fixed-window passages from four bundled documents and 1,520 explicitly
  synthetic units, not an exact production map-unit corpus.
- Shared tokenization: jieba 0.42.1 for Chinese and the production syntok
  fallback for English. BM25 `k1=1.5`, `b=0.75`; RRF `k=60`, weights 1/2.
- Corpus fingerprint remained
  `f02a50e7f6fbe18153f776595f1bf785e6483211232fb2072b26a555bfef2a14`.
  Judgment fingerprint:
  `ae8bceb2484e9d2116ba7a78be0d71fc1ac2012a7ba4b2207d4512dbac504461`.
- Read-only inspection of the route and engine implementation confirmed that
  compare calls run native `_msearch` and explanation calls native `_explain`.
  Compare is read-only despite using HTTP POST. Avoid the application's
  startup preparation routine: it can create/repopulate fixture indexes.

Observed host resources were two logical CPUs and `MemTotal: 12227892 kB`
(about 11.66 GiB). The lab README declares a 768-MiB JVM heap/1,792-MiB
container limit. Those container limits were not independently remeasured
in this check. This is not the accepted 4-vCPU/32-GiB target-node baseline.

## Executed checks

Ran six sequential existing-fixture comparisons with `topK=20`, then one
native explanation for the first case's leading result. Every response was
HTTP 200. All six comparisons exactly reproduced the frozen reference's
hit IDs/order, per-channel and RRF scores, candidate counts, overlap, and
relevance metrics; elapsed timers were excluded from equality checks.

| Fixture | Purpose | Live replay |
| --- | --- | --- |
| `public-00`, `data center revenue` | Bundled financial evidence | Matches frozen reference |
| `public-21`, `share voting` | Preserve the known provisional relevance regression | Matches frozen reference |
| `business-00` | Chinese synthetic business evidence | Matches frozen reference |
| `edge-common` | Common-term negative-IDF-floor control | Matches frozen reference |
| `edge-half` | `df=N/2` control | Matches frozen reference |
| `filter-530bd7ed`, `gross margin revenue` | Document allowlist with native index statistics | Matches frozen reference |

The native explanation returned both path/content explanations. The two
constructed IDF controls still have zero legacy hits and twenty native hits.
The retained `share voting` provisional nDCG@10 remains 0.804811 versus
0.795166. These facts reproduce mechanism/regression cases; they do not make
the provisional annotations reviewed business acceptance. The experiment
also includes native length/statistics differences, so it is not a controlled
IDF-only ablation.

Existing performance, retained-background write, and word-volume evidence
files all reported `measured` with completed timestamps and the same corpus
fingerprint. Their status was inspected; the benchmarks were not rerun or
accepted against the new workload. In particular, no 50-QPS/400-index test
or new open/close/bulk operation was performed here.

## How to reuse it for the next validation stage

1. Preserve the existing six fixture indexes, application, frozen judgments,
   and historical benchmark records as the comparison reference. Use the
   existing API for normal comparisons/explanations and the corpus/query
   manifests for reproducible inputs.
2. Run mutation cases in independently named temporary indexes, not the
   shared demo indexes. Confirm benchmark activity before a new load run;
   completed historical evidence alone does not reserve the host. The
   existing public comparison API has no revision/build or failure-injection
   operations; do not pretend it implements the new publication protocol.
3. Reuse prepared token fixtures for partial candidate visibility, failed
   executions, new physical retry builds, activation filters, and cleanup.
   Add these capabilities to an isolated validation harness when authorized;
   do not repopulate or force-merge the reference indexes to simulate them.
4. Preserve full channel inventories for mechanism comparisons. The lab
   currently fuses all matching hits, whereas the engineering design's
   candidate depth is a separate configurable qualification parameter.
   Measure candidate-window effects separately. The frozen clean fixture was
   force-merged during its original preparation; that result does not prove
   ranking quality during coexistence/deletion/merge transitions.
5. The reviewed twenty real-business queries and exact production map-unit
   inputs remain a preparation gate. The lab's provisional bundled/synthetic
   cases are reusable starting material, not automatic replacement evidence.
6. Qualify the accepted mixed workload and recovery objectives on the
   specified target configuration. Oracle measurements under its present
   constraints can inform mechanism/debug work and exploratory curves; they
   do not establish the 32-GiB node's capacity, AWS S3 costs, or RTO.

The existing deployed lab therefore supplies a working native lexical
reference and reusable inputs. Knowhere integration, cross-process fencing,
read lifetime/cleanup, cache coherence, SDK/webhook behavior, actual AWS
configuration, and the agreed business/load/recovery gates remain separate
from this successful six-case replay.

## Evidence

All files are under the captured evidence directory (`opensearch-lab-reuse-2026-11-03/`):

- HTTP ledger (`opensearch-lab-reuse-2026-11-03/http-ledger.json`): exact requests,
  statuses, timestamps, response file names, and SHA-256 fingerprints.
- Live summary (`opensearch-lab-reuse-2026-11-03/reuse-check-summary.json`),
  manifest (`opensearch-lab-reuse-2026-11-03/manifest.json`), and
  queries (`opensearch-lab-reuse-2026-11-03/queries.json`).
- Frozen replay equality (`opensearch-lab-reuse-2026-11-03/frozen-replay-check.json`)
  and reference extract (`opensearch-lab-reuse-2026-11-03/frozen-reference-extract.json`).
- Native explanation (`opensearch-lab-reuse-2026-11-03/explain.json`).
- Host/source/evidence inventory (`opensearch-lab-reuse-2026-11-03/environment-and-source.json`):
  read-only resource observation, current lab-source fingerprints, and
  historical evidence identities. No credentials are included.
