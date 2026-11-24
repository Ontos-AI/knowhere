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
- Heap and memory alarms are defined in `host-monitoring-stack.yaml` but not
  deployed yet. Deploy them and collect a baseline on the current node before
  resizing.
