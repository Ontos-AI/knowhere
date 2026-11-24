# Right-size the shared OpenSearch node

Date: 2026-11-23. **Read-only measurement. No AWS resource or node setting was
changed.** Scope: the shared node `knowhere-opensearch-shared`
(`r7g.xlarge`, 32 GiB, 16-GiB heap, OpenSearch 3.9.0, remote store on).

## Decision

Make the node size a manifest field and move the shared node to `r7g.large`
(16 GiB) with an 8-GiB heap. Compute falls from $156.37 to $78.18 a month at
on-demand prices; EBS (120 GiB gp3) is unchanged. Keep `r7g.xlarge` as the
default so existing manifests render the deployed size.

`t4g.large`/`m7g.large` (8 GiB, 4-GiB heap) are not chosen yet. They leave less
room for the growing shard count, and the one 80% CPU burst seen on four vCPUs
would saturate two.

## Measurements

CloudWatch, 2026-11-09..2026-11-23, and node APIs read with the monitor
certificate:

| Signal | Value |
| --- | --- |
| CPU, daily average | 0.38–1.21% |
| CPU, hourly maximum | p50 0.9%, p95 11.2%, max 80.0% (once, 2026-11-13) |
| Heap used | 8% of 16 GiB |
| Old generation | 286 MB after last GC, 724 MB peak |
| Parent circuit breaker estimate | 1.4 GB of a 15.1 GB limit, never tripped |
| Shards | 552 primary, all assigned; 541 open and 10 closed indexes |
| Index store | 287 MB in total; data volume 1.2 GB of 100 GB used |
| Searches | 401,803, 0.11 ms average query time |

The node README already records that old generation stayed at 0.27 GiB from 186
to 786 open shards on this node. The 8-GiB heap is about 5.7 times the current
breaker estimate.

## Prices

AWS Price List API, us-east-1, Linux, shared tenancy, on demand, read
2026-11-23:

| Instance | vCPU | Memory | $/hour | $/730 h |
| --- | ---: | ---: | ---: | ---: |
| `r7g.xlarge` | 4 | 32 GiB | 0.2142 | 156.37 |
| `r7g.large` | 2 | 16 GiB | 0.1071 | 78.18 |

A one-year standard no-upfront reservation for `r7g.large` is $0.0708/hour
($51.68/month). Commit only after the smaller node has run stably.

## Not covered

- Behaviour of the smaller node under the target 15-QPS retrieval burst or a
  large ingestion; only the idle-to-light production load above was observed.
- Restart time of 541 open indexes on two vCPUs.
- The independent full snapshot of the runbook's step 1: the shared node has no
  full-snapshot repository or backup timer, only the remote store. EBS snapshots
  of both volumes stood in for it.
- A small ingestion on the new node was not run by this change.

## Resize executed

2026-11-24, after the user approved the switch. The heap and memory alarms were
deployed on 2026-11-23 and had collected about one day of baseline on
`r7g.xlarge` (heap 31.7%, old generation 3.6%, available memory 39.5%).

| Step | Result |
| --- | --- |
| Pre-flight | Green; 552 shards; 541 open, 10 closed; 156,856 documents |
| Safety copy | EBS snapshots `snap-0cd8b9541b2ec9867` (data) and `snap-0b7bde32f2dddcf47` (root) |
| Heap | Drop-in `/etc/systemd/system/knowhere-opensearch.service.d/heap.conf` sets `-Xms8g -Xmx8g`; `daemon-reload` only |
| Change set | `resize-r7g-large-20261124` on the live template, `Host.InstanceType` only; `DataAttachment` was listed as a dynamic reference and was not updated |
| Execution | 12:24:27 to 12:25:11 UTC; same instance ID and private IP |
| Recovery | Service active at 12:25:05; green at 12:29:03 |
| After | 541 open, 10 closed, 552 shards, 156,856 documents; heap maximum 8 GiB, `MaxDirectMemorySize` 4 GiB; old generation 321 MB; available memory 37% of 15.3 GiB; all seven alarms OK |

Retrieval was unavailable for about four and a half minutes: from the stop
at 12:24:30 until green at 12:29:03. Watch the next 1–2 weeks of
heap, old generation and memory before buying a one-year commitment.

Rollback: create the same change set with `r7g.xlarge`, remove `heap.conf`
and run `daemon-reload` before executing it.
