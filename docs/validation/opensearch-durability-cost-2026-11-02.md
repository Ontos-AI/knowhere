# OpenSearch remote-store benefit and cost assessment

Raw evidence files named in this document (JSON, CSV, SQL, logs and probe copies) are retained privately and are not published in this repository.

Date: 2026-11-02. **Assessment only; remote-backed storage is not accepted or deployed.** Scope: official self-managed OpenSearch 3.9.0, not Amazon OpenSearch Service. The [durability research](opensearch-durability-2026-11-02.md) establishes the recovery mechanism; this report evaluates whether buying it is worthwhile.

## Recommendation and decision

**Do not adopt remote store automatically.** Its specific benefit is preserving acknowledged lexical writes when the only node's data disk is lost. That is valuable if losing changes since the last complete snapshot is unacceptable. If that loss window is acceptable, local durable storage plus the already accepted snapshots is operationally simpler. Sparse workload observations suggest no obvious need to buy additional indexing throughput; they do not establish the S3 bill.

The unresolved business decision is: **after permanent loss of the only OpenSearch disk, may Knowhere lose lexical writes since the latest successful snapshot, or must every acknowledged write survive?** Recovery speed and service availability are separate requirements. Internal targets remain 15 minutes with retained disk and one hour from S3, both unverified.

PostgreSQL lexical data is temporary migration rollback only, then stopped/deleted. It supplies no ongoing backup, replay, or reconstruction. Canonical bodies and other application data remaining in PostgreSQL are not a disaster-recovery source in this assessment.

## What the additional expense buys

| Option | Disk survives | Only data disk is permanently lost | Availability and cost consequence |
| --- | --- | --- | --- |
| Local `request` durability + hourly full snapshots | Recover local acknowledged operations after restart | Recover captured shard states from the latest complete snapshot; later writes can be lost | Single-node outage; accepted baseline plus snapshot expense |
| Local storage + shorter snapshot interval | Same local recovery | Smaller intended loss window, still nonzero | More snapshot scheduling/metadata work; duration and failed runs can extend actual snapshot age |
| Remote segments + `request` translogs + remote cluster state + independent full snapshots | Adds little durability benefit to ordinary retained-disk restart | Officially supports recovery through the last acknowledged write regardless of replica count | Adds S3 to acknowledgment/recovery dependencies; replacement and restoration still required |

The remote guarantee requires `index.translog.durability=request`; async acknowledgment does not supply it. Remote store does not make this one-node service highly available or establish a faster RTO. It offers no demonstrated normal-search latency benefit here. Published OpenSearch throughput benchmarks used three nodes and replicas, unlike this candidate. [Remote-store guarantees and benchmark topology](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/remote-store/index/)

Remote state protects metadata needed to interpret remote data. A replacement must still recover the installation, access, cluster metadata and shards. Mistaken updates/deletes remain changes to current state, so retain the separate accepted hourly snapshots, seven-day retention, and minimum three complete coverage-verified recovery points. Full snapshots must remain independent: shallow snapshots reference live remote segments. [Remote state](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/remote-store/remote-cluster-state/), [shallow snapshots](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/remote-store/snapshot-interoperability/)

Adding a replica requires another data node and does not itself solve manager quorum. Two voting managers cannot tolerate losing either voter; an availability design needs an appropriate quorum and failure placement. One additional identically provisioned node alone adds roughly $165.97/month before network and other charges. This is a different purchase from remote durability. [Voting and quorum](https://docs.opensearch.org/latest/tuning-your-cluster/discovery-cluster-formation/voting-quorums/)

## Known baseline and missing denominators

The supplied candidate is one private us-east-1 Linux ARM64 `r7g.xlarge`, 4 vCPU/32 GiB, 16 GiB heap, 100 GiB gp3 data plus 20 GiB root, 3,000 IOPS/125 MiB/s. At $0.2142/hour × 730 plus 120 × $0.08/GiB-month, compute/EBS is **$165.97/month**, excluding all S3, snapshots, monitoring, network and worker expense. Remote store does not justify reducing this local disk allocation. Compute pricing was rechecked through AWS Price List on this date; see sanitized price evidence (`opensearch-durability-cost-2026-11-02/pricing.json`). [EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/), [EBS pricing](https://aws.amazon.com/ebs/pricing/)

There are about 370 namespace indexes, one primary/zero replicas each; most have fewer than 50 units and are closed while idle, largest about 45,000. Fourteen observed days contain 217 successful private ingestions and 5,327 units: median 1, p95 about 147, maximum 482 units/job; peak 549 units/second and 991/minute. Twenty-seven demo jobs are excluded; retries/deletions are undercovered. Target arrivals are three times the actual burst, with a **15 external-retrieval-QPS burst**, not continuous traffic.

No actual OpenSearch footprint, payload bytes, object count, churn, or S3 request count has been measured. The 14.5 GiB PostgreSQL table is not an OpenSearch/S3 sizing proxy. These partial job observations cannot support a universal monthly estimate.

## Why writes and units do not equal S3 requests

- Translog sync rotates a generation and uploads outstanding data, then transfer metadata. Checkpoints can be carried as object metadata or separate files. The pinned implementation also sequences a conditional fence PUT after metadata before completing the sync. Its default request-sync buffer is 650 ms; batching changes operation/request ratios while acknowledgment still waits. [Translog rotation](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/index/translog/RemoteFsTranslog.java#L551-L643), [transfer sequence](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/index/translog/transfer/TranslogTransferManager.java#L199-L319), [buffer default](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/index/IndexSettings.java#L866-L874)
- Segments arise from refresh, flush and merges; merges create replacement bytes, not merely newly ingested text. Segment metadata, retries, multipart requests and cleanup add activity. Closed shards skip segment synchronization, and unchanged/in-sync refreshes need not upload. Multiplying 370 indexes by a continuous upload timer would overstate activity. [Refresh decisions](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/index/shard/RemoteStoreRefreshListener.java#L155-L198), [multipart implementation](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/plugins/repository-s3/src/main/java/org/opensearch/repositories/s3/S3BlobContainer.java#L740-L839)
- State updates upload changed metadata and manifests; index creation/open/close/mapping activity matters independently of units. Scheduled state cleanup defaults to 300 seconds but checks state-version progress before remote listing; it is not a guaranteed PUT every five minutes. [Incremental metadata](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/gateway/remote/RemoteClusterStateService.java#L394-L508), [committed manifest](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/gateway/remote/RemoteClusterStateService.java#L988-L1047), [cleanup gating](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/gateway/remote/RemoteClusterStateCleanupManager.java#L179-L205)

## Incremental AWS cost, separate from snapshots

Verified us-east-1 S3 Standard prices: first 50 TiB **$0.023/GB-month**; PUT/COPY/POST/LIST **$0.005/1,000**; GET/HEAD **$0.0004/1,000**. AWS uses binary GB, equivalent to GiB. DELETE/CANCEL requests are free; listing/reading during cleanup still counts. Price List SKUs: storage `WP9ANXZGBYYSGJEA`, request tiers `E9YHNFENF4XQBZR6` and `ZWQ6Q48CRJXX4FXE`. [public regional price catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/us-east-1/index.json), sanitized query evidence (`opensearch-durability-cost-2026-11-02/pricing.json`), [AWS S3 billing semantics](https://aws.amazon.com/s3/pricing/)

For monthly average retained live remote storage `G` GiB, billable PUT-class/LIST requests `P` million and GET/HEAD requests `R` million:

```text
Incremental remote AWS cost = 0.023 × G + 5 × P + 0.4 × R
                           + network + KMS + monitoring/other enabled features
Total candidate cost = 165.97 + independent snapshot cost + incremental remote cost
```

`G` includes segments, retained translogs/state, stale generations awaiting cleanup and any retained object versions. Upload bytes and stored bytes differ: rewrites/churn and cleanup determine both. Use actual multipart, fencing, retry and listing requests in `P/R`, not one request per unit.

| Illustrative sensitivity, not forecast | Average remote GiB | PUT-class/LIST million | GET/HEAD million | Incremental S3/month |
| --- | ---: | ---: | ---: | ---: |
| A | 10 | 0.1 | 0.1 | $0.77 |
| B | 50 | 1 | 1 | $6.55 |
| C | 100 | 10 | 10 | $56.30 |

These rows exclude independent snapshots, optional features and temporary migration/recovery/test capacity. They show request amplification can outweigh storage; none is a measured Knowhere estimate.

Read-only AWS inspection (`opensearch-durability-cost-2026-11-02/network-summary.json`) found an available S3 Gateway endpoint in the production VPC, already associated with both current private API/worker subnet route tables. A future node using those tables and ordinary regional S3 endpoints can avoid S3 NAT processing and new endpoint fixed charges. The candidate node/bucket do not exist; access policy/DNS connectivity remains untested. Gateway endpoints have no additional charge and same-Region S3-to-EC2 data transfer has no transfer charge. [S3 Gateway endpoints](https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-s3.html), [S3 transfer pricing](https://aws.amazon.com/s3/pricing/)

SSE-S3 adds no encryption fee. SSE-KMS introduces key/request expense and decrypt/authorization dependencies; calculate separately if selected. [SSE-S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingServerSideEncryption.html), [SSE-KMS](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html)

Full snapshots are incremental within their own repository: unchanged files are reused, so 168 hourly snapshots do not imply 168 complete data copies. Independent snapshots still have their own retained-byte, request and metadata costs. Shortening the interval increases opportunities to capture changes and metadata activity; it is not priced by simply multiplying all data bytes by frequency. [Snapshot behavior](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/snapshots/snapshot-restore/), [file reuse](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/repositories/blobstore/BlobStoreRepository.java#L3998-L4081)

## Operational expense and next step

Remote store adds repository/IAM management, S3 upload lag/failure monitoring, garbage-collection and retention checks, version/plugin compatibility, fencing and replacement procedures, and a separate snapshot restore path. S3 latency/errors now affect write acknowledgment. Operator time is unpriced and could exceed the small illustrative AWS bills.

Choose the disk-loss RPO first. If acknowledged-write survival is required, the additional durability is a strong functional fit, conditional on affordable measured request volume and proven recovery. If snapshot-age loss is acceptable, keep the simpler baseline and assess a shorter interval only if necessary.

Only if cost uncertainty changes that decision, propose one separately approved validation with a fixed representative ingest/update/delete/open/close trace, a bounded idle window and an explicit spend/time ceiling; no ongoing percentile campaign. Record retained bytes/objects, billable request classes, native upload bytes/counts and acknowledgment behavior, then reconcile them. Native remote-store counters are not S3 billing counters. [Remote Store Stats API](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/remote-store/remote-store-stats-api/)

No application code, infrastructure changes, paid tests or uploads were performed for this assessment. Workload-specific cost, latency and both recovery targets remain unverified.
