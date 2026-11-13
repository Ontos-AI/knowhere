# OpenSearch underlying compatibility PoC

Raw evidence files named in this document (JSON, CSV, SQL, logs and probe copies) are retained privately and are not published in this repository.

Date: 2026-11-03, America/Los_Angeles.

Status: completed bounded Oracle functional PoC. All seven cases passed with
S3 emulation. Remote import/argument-parser and lint checks passed. This is
underlying functional evidence, not production or actual AWS acceptance.

The successful run was `2026-11-03T11:03:51.879756+00:00` through
`2026-11-03T11:08:47.899994+00:00` (03:03:51 through 03:08:47 Pacific).

## Result

The pinned single-node configuration successfully combined remote segments,
request-durable translogs, remote cluster state, close/open, and independent
full snapshots on the synthetic fixture. Removing the only OpenSearch
container and its complete local data volume did not lose the acknowledged
tail changes. A second recovery used only the full backup after the source
live-store process and its in-memory data were removed.

This supports continuing to the accepted integration stage. Actual AWS
qualification and the target-node capacity/recovery gates remain required.

## Scope

This is stage 1 of the accepted feasibility plan. Test the combination of
one ARM64 OpenSearch node, zero replicas, remote segments, request-durable
remote translogs, remote cluster state, namespace close/open, and an
independent full-snapshot repository. Use only synthetic records and
OpenSearch-owned recovery payloads. PostgreSQL remains part of the intended
application architecture but supplies no lexical recovery data to this probe.

The owner requested the Oracle lab host and no test backend on the Mac.
The temporary Mac VM was removed before test containers started; see
local cleanup (`opensearch-compatibility-2026-11-03/local-cleanup.json`).

## Environment and isolation

The existing Oracle retrieval lab is at
`<lab-home>/knowhere-retrieval-lab`. Its separate comparison currently uses
PostgreSQL and OpenSearch. The initial runtime protocol is a frozen checkpoint;
the actual completion record is `data/performance-evidence.json`. It reported
`measured` with completion at `2026-11-03T10:50:54.111333+00:00`, and no active
benchmark process remained before the new probe started. The comparison's
indexes, data, and running services are preserved.

The compatibility workspace is
`<lab-home>/knowhere-compatibility-20261103-c1a7`.
The standalone [probe](../../scripts/probes/verify-opensearch-compatibility.py)
uses uniquely named containers, data volumes, a dedicated Docker bridge network,
and loopback-only published ports. It records baseline container state and
checks its cleanup and preservation afterward.

The Oracle host has two logical CPUs and approximately 12 GiB RAM. Configured
test limits were a 768-MiB fixed JVM heap in a 1,792-MiB OpenSearch container,
limited to one CPU. Each Moto container had a 384-MiB memory and 0.25-CPU
limit; at most two stores ran concurrently. The probe used the lab's Python
3.14.2 interpreter. These resources differ from the accepted 32-GiB
production-validation baseline.

The cached OpenSearch image on Oracle was inspected and matches the official
ARM64 configuration digest
`sha256:4fc0ee89d630dfe0d6d330445d651d00ccc066c604f91801cbcdc387901f22f5`.
The probe derives a temporary image with `repository-s3` from the pinned
official 3.9.0 OCI index. Runtime version and build hash must also match.
See the [official identity and compatibility review](opensearch-compatibility-official-2026-11-03.md).

S3 is emulated by pinned Moto 5.1.18 ARM64 containers. Source live storage,
backup storage, and fresh target live storage use independent processes.
Moto's [server mode](https://docs.getmoto.org/en/latest/docs/server_mode.html)
supports AWS SDK clients, but does not certify AWS behavior. Its preserved
in-memory service state models an available remote store while the
OpenSearch container and local volume are destroyed; it does not simulate
an Oracle host power loss or certify S3 object durability.

## Cases and acceptance

| Case | Required evidence | Result |
| --- | --- | --- |
| Remote-backed writes and lexical visibility | Explicit request durability, remote-store and segment settings, whitespace/BM25 mapping, exact records and lexical matches | Passed; 24 records across three namespace indexes |
| Close/open | Closed reads blocked; reopened exact records and search work | Passed |
| Full snapshot with a closed namespace | Explicit full flags; complete three-index inventory and successful shards | Passed; `SUCCESS`, three successful shards, zero failures |
| Loss of only node and all local data | Acknowledged unrefreshed/unflushed create/update/delete ledger recovered; new cluster UUID; metadata and closed state preserved | Passed; exact 27-record ledger, including four creates, one replacement, and one delete |
| Writes after remote recovery | A new write completes `refresh=wait_for` and is found by search | Passed |
| Source-independent full restore | Source live-store process unavailable; fresh target repositories; exact historical snapshot content and aliases recovered | Passed; exact earlier 24-record snapshot, with later changes absent |
| Writes after full restore | A new write completes `refresh=wait_for` and is found by search | Passed |

The fixture used three namespaces with eight initial records each. The
snapshot was taken while one namespace was closed. Later tail changes created
four records, replaced one, and deleted one with refresh disabled; refresh and
flush counters remained unchanged before killing the original node: refresh
was 7 before/after and flush was 1 before/after. Remote recovery restored
those changes; historical snapshot recovery restored the earlier snapshot
state instead. Record contents and revisions, whitespace analysis, BM25
parameters, repository selection, and aliases were checked after recovery.

The original cluster UUID was `uSRahXUtTjah2JxwdCdOFg`; the clean remote-state
replacement was `PVvVC1KqQrybvihsqRdywA`. No index was recreated from PostgreSQL.
Closed metadata state was preserved by remote-cluster-state recovery.
Ordinary snapshot restore opened the restored indexes for verification;
it does not establish automatic preservation of idle/closed lifecycle state.
Recovery scripts must apply the accepted batch/admission/LRU policy rather
than assuming every historical index should stay open after restoration.

## Limits

A functional pass here establishes only this pinned build's behavior
under the stated fixture and S3 emulation. It does not accept actual AWS
IAM/TLS/S3 behavior, encryption/key recovery, the Knowhere publication
integration, business retrieval quality, capacity, cost, or the internal
15-minute/one-hour end-to-end recovery objectives. The existing lab's
comparison results remain separate evidence. This run does not validate
370/400 simultaneously open namespaces, the 45,000-unit namespace, mixed
load, hourly scheduling/seven-day retention/minimum-three-success cleanup,
or an S3 outage/permission-denial fault campaign. The test's runtime is not
an end-to-end recovery-time result.

The first preparation attempt stopped before OpenSearch started because this
host's `--internal` Docker network did not publish the requested loopback
port. A separate controlled diagnostic reproduced the missing port mapping
on an internal network and HTTP 200 on an ordinary bridge. The fixture was
changed to a dedicated bridge with explicit loopback-only bindings. Both
attempt and diagnostic resources were removed; original containers were
unchanged. This setup result is not an OpenSearch compatibility failure.

## Evidence, reproducibility, and cleanup

- Successful case results and cleanup (`opensearch-compatibility-2026-11-03/oracle/result.json`).
- HTTP responses and metadata (`opensearch-compatibility-2026-11-03/oracle/http-evidence.json`).
- Acknowledged tail, unchanged refresh/flush counters, and expected ledger (`opensearch-compatibility-2026-11-03/oracle/acknowledged-tail.json`).
- Executed source identity and lint result (`opensearch-compatibility-2026-11-03/oracle/executed-static-checks.json`).
- Exact executed probe (`opensearch-compatibility-2026-11-03/oracle/executed-probe.py`).
- First preparation attempt (`opensearch-compatibility-2026-11-03/oracle/attempt-1-setup/result.json`) and network diagnostic (`opensearch-compatibility-2026-11-03/oracle/network-diagnostic.json`).
- Final resource cleanup (`opensearch-compatibility-2026-11-03/oracle/final-cleanup-check.json`).

The executed probe SHA-256 is
`d94f1468fa24b1144d5164343d6fae0a47ee6fb591464c4b0c73cfa6be0e143f`,
matching the repository probe and retained executed copy.

Reproduction on the approved Oracle host, after the comparison reports
`measured`, used:

```sh
cd <lab-home>/knowhere-compatibility-20261103-c1a7
<lab-home>/knowhere-retrieval-lab/.venv/bin/python compatibility-probe.py \
  --work-directory <lab-home>/knowhere-compatibility-20261103-c1a7 \
  --benchmark-protocol <lab-home>/knowhere-retrieval-lab/data/performance-evidence.json
```

All eighteen pre-existing containers retained their recorded state during
the run. Generated containers, data volumes, network, and the temporary
derived image were removed. The evidence directory and shared vendor-image/
builder cache were retained. No production operation or AWS resource
provisioning was performed.
