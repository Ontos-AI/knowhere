# Source content deduplication review and ablations

Reviewed on 2026-10-10 against `5264697481567b76abf8abcf463573018847701c`, including the uncommitted implementation and new files. The specification is the agreed conversation: compare original bytes within one user and namespace, reject unchanged updates, use current active revisions and in-progress reservations, and perform no historical backfill.

## Standards

The review found one concrete migration defect: a valid, ready index with the expected name but incorrect columns or predicate was retained. Two real PostgreSQL contracts reproduced the defect under ordinary and caller-owned transactions. The migration now validates the table, nonunique B-tree definition, column order and expression, predicate, validity, and readiness before retaining an index. Twelve cases pass, including retention of the correct index without rebuilding it and replacement of indexes whose JSON keys differ in case or contain a cast-like suffix. Definition comparison preserves JSON string literals rather than normalizing their case or stripping cast-like text from them.

No remaining blocker was found. Optional improvements include a direct interrupted-index recovery contract, concurrent index removal during ordinary downgrade, and a precise fingerprint return type. Canonical index-definition formatting is verified on the local PostgreSQL version; a future formatting change could cause an unnecessary rebuild.

## Spec

No blocking behavior mismatch was found. Publication and the terminal job transition commit together, accepted fingerprints survive ordinary metadata updates, and completed task redelivery exits before admission.

The review identified a lost export contract. It is restored for two same-named XLSX files differing only in ZIP comments: both succeed, retain full document and ZIP chunks, and produce identical parsed chunk identifiers. The duplicate worker contract now enables billing and verifies that the duplicate leaves account balance unchanged, has no result, and creates an `ALREADY_EXISTS` webhook outbox event and dispatch request. The dispatch is captured locally; external webhook delivery is not exercised.

Exact byte equality remains the agreed boundary. Re-exported files with changed container metadata can pass even when their visible content is identical. An unchanged source is also rejected when only parsing options change. Existing unhashed sources remain outside admission.

## Component ablations

Every variant ran the same 14 admission contracts against an isolated real PostgreSQL fixture and local source files. Mutations were test-scoped patches in separate processes; production code was not replaced with ablated implementations. All runs below had zero setup errors. Failures in the altered variants are intentional evidence that the contracts detect the removed behavior.

| Variant | Passed | Failed | Observed failure |
| --- | ---: | ---: | --- |
| Unchanged implementation | 14 | 0 | Agreed behavior holds |
| Use file size instead of hashing bytes | 12 | 2 | Different bytes of equal length are falsely rejected |
| Omit fingerprint persistence | 5 | 9 | Later duplicates are accepted and durable fingerprints are unavailable |
| Remove advisory transaction lock | 13 | 1 | Both concurrent claims are admitted |
| Check published documents only | 12 | 2 | Unpublished reservations and the publication transition are missed |
| Split the union into two separate reads | 13 | 1 | Publication between the snapshots allows a duplicate |

The concurrency contract pauses the first transaction after its empty lookup and starts the second claim before releasing it. With the lock present, PostgreSQL reports the second transaction waiting for its advisory lock. Without the lock, the second claim can finish against the empty state. The publication contract commits publication after the first lookup executes, exposing the gap in separate statement snapshots.

## Index ablation and skew experiment

The experiment used the actual ORM union query and the ordinary runtime database role, with planner defaults enabled. The first dataset contained 5,000 active current document revisions with distinct hashes and one incoming job. The last dataset additionally contained 2,000 running jobs with the requested hash in other namespaces. Every query correctly returned no duplicate in the incoming namespace.

| Local workload | SQL execution time | Shared buffer hits | Query-plan evidence |
| --- | ---: | ---: | --- |
| Hash index, absent hash | 0.093 ms | 6 | Published branch uses user/hash index; no revision joins execute |
| Same dataset, hash index removed | 37.883 ms | 30,210 | Visits 5,000 documents and performs 5,000 result/job lookups |
| Hash index, same hash in 2,000 other namespaces | 6.948 ms | 4,177 | Published branch probes 2,000 candidate results; processing branch filters 2,001 jobs |

These are single local `EXPLAIN (ANALYZE, BUFFERS)` probes with zero shared disk reads, not production latency measurements or a five-million-document benchmark. SQL execution time excludes download, hashing, pool acquisition, network round trips, and lock waits. Buffer counts and visited rows provide the stronger evidence.

The index is necessary, but matching hash multiplicity still matters because namespace and lifecycle state are later filters. The earlier 5–20 ms planning estimate assumes few matching user/hash rows and is not a worst-case guarantee. We keep the current design for this change; a workload dominated by repeated content across namespaces would justify revisiting index scope and the published-candidate query. Terminal and archived history were not measured by this skew experiment.

## Reproduction and validation

Local experiment artifacts are in `/tmp/knowhere-source-content-ablation/`: `source_content_ablation.py`, `run_matrix.py`, per-variant logs and JUnit reports, `results.json`, `test_query_plan_experiment.py`, and the complete `query-plans.json`.

The matrix can be repeated with `python3 /tmp/knowhere-source-content-ablation/run_matrix.py`; it uses the existing uv environment, applies one mutation per process, and runs the admission contract file. Local PostgreSQL sockets must be permitted. The index experiment loads `contract.conftest` as a pytest plugin, executes its temporary experiment file, and drops/recreates the hash index only in the isolated fixture database.

The strengthened worker suite passes all 33 contracts, and the API and migration suite passes all 61 contracts. The final expanded index migration check passes all 12 cases; this check overlaps the broader migration suite and is not an additional 12 distinct contracts. Ruff, Pyright (production paths, changed API contracts, and the new migration), and `git diff --check` pass. URL transport and real external webhook delivery remain outside these local contracts.

Before PR publication, the change was rebased onto `main` at `65c0445a` and revalidated: all 33 worker contracts and all 65 API/migration contracts pass, including the expanded migration cases. Ruff, Pyright, and the diff whitespace check also pass on that base.

Standards: one confirmed defect, fixed; optional operational and typing suggestions remain. Spec: no blocking mismatch; export, billing, webhook, and concurrency coverage strengthened.
