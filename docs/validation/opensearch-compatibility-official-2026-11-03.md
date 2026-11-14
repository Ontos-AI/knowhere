# OpenSearch 3.9.0 compatibility: official evidence

Raw evidence files named in this document (JSON, CSV, SQL, logs and probe copies) are retained privately and are not published in this repository.

Research date: 2026-11-03. Scope: primary documentation, tagged upstream source and tests, and read-only official image-registry metadata. This note does not report a successful runtime experiment. A temporary Mac VM was prepared separately, then removed before any test containers started after the owner requested the existing Oracle-host comparison environment instead. See the local cleanup record (`opensearch-compatibility-2026-11-03/local-cleanup.json`). The owner confirmed the Oracle lab host; the exact sidechat remains unconfirmed. Runtime results are recorded separately, and validation on the Mac is outside the current scope.

## Assessment

The proposed combination has no documented fundamental incompatibility: one ARM64 node, zero replicas, remote segments and request-durable remote translogs, remote cluster state, index close/open, and ordinary full snapshots. This establishes a valid investigation target, not a completed durability claim. The upstream zero-replica restore tests and full-cluster metadata recovery tests use multiple nodes, so they do not substitute for the exact one-node experiment. [Remote-store durability contract](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/remote-store/#potential-use-cases), [zero-replica restore test](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/internalClusterTest/java/org/opensearch/remotestore/RemoteStoreRestoreIT.java#L127-L144), [full-cluster restore test](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/internalClusterTest/java/org/opensearch/remotestore/RemoteStoreClusterStateRestoreIT.java#L107-L172).

Two recovery mechanisms must be proved separately:

1. **Current-state disaster recovery:** retain live remote segment, translog, and state repositories; replace the stopped source node with an empty local data path; recover metadata and the last acknowledged writes.
2. **Independent backup recovery:** retain only a full snapshot repository from the source; restore into a fresh node with fresh target remote repositories and no access to source live repositories. This proves snapshot independence and a historical recovery point.

OpenSearch recovery itself does not invoke PostgreSQL in the inspected paths. The PoC must recover lexical payloads and OpenSearch index metadata from OpenSearch-owned storage. Knowhere still retains authorization, authoritative publication metadata, bodies, vectors, assets, and citations in their agreed PostgreSQL/application placement; removing those dependencies is not the design. Stage 1 checks synthetic lexical records, index inventory, mappings, and aliases. Correct application-level revision selection and result hydration require the later integration stage. [Remote recovery implementation](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/index/shard/StoreRecovery.java#L655-L696), [full snapshot recovery implementation](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/index/shard/StoreRecovery.java#L894-L961).

## Exact official image identity

The following identities were retrieved directly from the public Docker Registry API, then cross-checked against Docker Hub's tag API on the research date. They describe the official image, not a cached local image. A runtime whose image configuration digest differs must not be presented as this verified official image.

| Item | Verified value |
| --- | --- |
| Tag | `opensearchproject/opensearch:3.9.0` |
| OCI multi-platform index | `sha256:3b55f6b6249cfdb92b52ee59adc8a266203ba9ef9f5d82544fad6f1af2f3609e` |
| Linux ARM64 image manifest | `sha256:bbdf65cb3d35c61a8ba495f22c1a42d7d7f0df23412e3419bf1e6cfca3ba4774` |
| Linux ARM64 configuration | `sha256:4fc0ee89d630dfe0d6d330445d651d00ccc066c604f91801cbcdc387901f22f5` |
| Configuration platform | `linux/arm64` |
| Configuration creation time | `2026-10-26T19:58:03.003661387Z` |
| Tagged OpenSearch source commit | `4ee42a94e87f66fbf1e62a9871b1b87f91e02472` |

The ARM64 manifest can be pinned as `opensearchproject/opensearch@sha256:bbdf65cb3d35c61a8ba495f22c1a42d7d7f0df23412e3419bf1e6cfca3ba4774`. Registry manifest requests require an ordinary public pull token; no account credential is needed. Primary endpoints: [Docker Hub tag metadata](https://hub.docker.com/v2/repositories/opensearchproject/opensearch/tags/3.9.0), [OCI index](https://registry-1.docker.io/v2/opensearchproject/opensearch/manifests/sha256:3b55f6b6249cfdb92b52ee59adc8a266203ba9ef9f5d82544fad6f1af2f3609e), [ARM64 manifest](https://registry-1.docker.io/v2/opensearchproject/opensearch/manifests/sha256:bbdf65cb3d35c61a8ba495f22c1a42d7d7f0df23412e3419bf1e6cfca3ba4774), [ARM64 configuration blob](https://registry-1.docker.io/v2/opensearchproject/opensearch/blobs/sha256:4fc0ee89d630dfe0d6d330445d651d00ccc066c604f91801cbcdc387901f22f5), [official Git tag reference](https://api.github.com/repos/opensearch-project/OpenSearch/git/ref/tags/3.9.0).

The official Docker guide uses `discovery.type=single-node` for a single container. ARM64 availability is verified by the manifest above, but repository plugin availability and actual runtime identity still require inspection of the chosen host. [Docker installation guide](https://docs.opensearch.org/latest/install-and-configure/install-opensearch/docker/#run-opensearch-in-a-docker-container).

## Configuration that matches the durability target

The following is a configuration shape, with placeholder storage locations. It is not a deployment change. Remote-store attributes belong in `opensearch.yml` before first bootstrap and must agree across all nodes. Install or verify the `repository-s3` plugin when using S3 repositories. [Remote store configuration](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/remote-store/#configuring-remote-backed-storage), [S3 plugin setup](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/snapshots/snapshot-restore/#amazon-s3).

```yaml
cluster.name: compatibility-poc
node.name: compatibility-poc-node
node.roles: [cluster_manager, data, ingest]
discovery.type: single-node

cluster.remote_store.state.enabled: true

node.attr.remote_store.segment.repository: live-segments
node.attr.remote_store.translog.repository: live-translogs
node.attr.remote_store.state.repository: live-state

node.attr.remote_store.repository.live-segments.type: s3
node.attr.remote_store.repository.live-segments.settings.bucket: <live-bucket>
node.attr.remote_store.repository.live-segments.settings.base_path: <unique-run>/segments
node.attr.remote_store.repository.live-segments.settings.region: us-east-1

node.attr.remote_store.repository.live-translogs.type: s3
node.attr.remote_store.repository.live-translogs.settings.bucket: <live-bucket>
node.attr.remote_store.repository.live-translogs.settings.base_path: <unique-run>/translogs
node.attr.remote_store.repository.live-translogs.settings.region: us-east-1

node.attr.remote_store.repository.live-state.type: s3
node.attr.remote_store.repository.live-state.settings.bucket: <live-bucket>
node.attr.remote_store.repository.live-state.settings.base_path: <unique-run>/state
node.attr.remote_store.repository.live-state.settings.region: us-east-1
```

Remote-state persistence requires `cluster.remote_store.state.enabled: true`; merely specifying the state repository is insufficient. Remote publication and a routing-table repository are separate features, not prerequisites for basic remote-state disaster recovery. [Remote-state settings](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/remote-store/remote-cluster-state/#configuring-the-remote-cluster-state).

For a local S3-compatible endpoint, the tagged plugin accepts `s3.client.default.endpoint`, `s3.client.default.protocol`, `s3.client.default.region`, and `s3.client.default.path_style_access`. Do not embed credentials in this document or node attributes; supply them through the host's authorized credential mechanism or OpenSearch keystore. `s3_async_client_type` is a **repository setting** with accepted values `crt` and `netty`; a node-level occurrence alone does not select the repository client. An explicit Netty choice, if needed by the existing experiment, belongs at `node.attr.remote_store.repository.<name>.settings.s3_async_client_type: netty`, and in the snapshot repository definition. [Tagged client settings](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/plugins/repository-s3/src/main/java/org/opensearch/repositories/s3/S3ClientSettings.java#L120-L132), [path-style and region settings](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/plugins/repository-s3/src/main/java/org/opensearch/repositories/s3/S3ClientSettings.java#L238-L257), [repository client setting](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/plugins/repository-s3/src/main/java/org/opensearch/repositories/s3/S3Repository.java#L178-L187), [client-selection documentation](https://docs.opensearch.org/latest/api-reference/snapshots/create-repository/#s3_async_client_type).

Use explicit per-index settings for the acceptance fixture:

```json
{
  "settings": {
    "index.number_of_shards": 1,
    "index.number_of_replicas": 0,
    "index.replication.type": "SEGMENT",
    "index.translog.durability": "request"
  }
}
```

The bootstrap attributes automatically populate `index.remote_store.enabled`, `index.remote_store.segment.repository`, and `index.remote_store.translog.repository`. Inspect the resulting settings instead of assuming this happened. `SEGMENT` is required when remote data storage is enabled. Request durability is also the tagged default, but explicitly asserting it makes the experiment reviewable. [Automatic index defaults](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/cluster/metadata/MetadataCreateIndexService.java#L1283-L1368), [replication validation](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/cluster/metadata/IndexMetadata.java#L357-L425), [durability default](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/index/IndexSettings.java#L194-L200).

Do not use `node.attr.remote_store.mode: segments_only` for this target: it explicitly omits the remote translog requirement and therefore does not establish last-acknowledged-write recovery from remote translogs. In the default mode, the tagged node-attribute validator requires segment, translog, and state repository names together when remote data attributes are present. [Mode validation](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/node/remotestore/RemoteStoreNodeAttribute.java#L195-L232).

## Zero replicas and one node

The documented durability claim explicitly covers all replica counts when translog durability is `request`. Tagged tests also exercise manual remote restoration with zero replicas. However, a one-node outage removes every serving node: remote storage preserves a recovery source, not continuous availability. The exact duration and correctness of replacing the only node remain runtime acceptance items. [Durability contract](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/remote-store/#potential-use-cases), [zero-replica test](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/internalClusterTest/java/org/opensearch/remotestore/RemoteStoreRestoreIT.java#L127-L144).

Automatic remote restoration is not implied. In tagged 3.9.0, `index.remote_store.fencing.enabled` and `index.remote_store.auto_restore.enabled` both default to false. Enabling auto-restore requires fencing, and fencing requires conditional writes in the remote translog repository. The manual recovery experiment should stop the original writer before recovery; it does not prove behavior with a partitioned writer still alive. [Fencing and auto-restore definitions](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/cluster/metadata/IndexMetadata.java#L447-L515), [upstream zero-replica auto-restore test](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/internalClusterTest/java/org/opensearch/remotestore/RemoteStoreAutoRestoreIT.java#L160-L202).

## Closing and opening namespace indexes

Use explicit physical index names for close/open and backup inclusion:

```http
POST /namespace-a,namespace-b/_close?wait_for_active_shards=1
POST /namespace-a,namespace-b/_open?wait_for_active_shards=1
```

Close blocks reads and writes and reduces in-memory indexing/search structures. It retains index data and disk usage; it is not an eviction or remote-only storage mechanism. The normal shard recovery process runs during reopening. For one node and zero replicas, `wait_for_active_shards=1` is appropriate; values greater than one require additional copies. [Close Index API](https://docs.opensearch.org/latest/api-reference/index-apis/close-index/).

Default snapshot wildcard expansion is open/hidden. Therefore a request without `indices` can silently omit closed namespace indexes. Explicit names include closed indexes; alternatively set `expand_wildcards: "all"` in the create-snapshot body when intentionally using a wildcard. The tagged request parses indices options from the body, and the upstream test successfully snapshots a closed index with expanded options. [Create Snapshot API](https://docs.opensearch.org/latest/api-reference/snapshots/create-snapshot/), [tagged defaults](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/action/admin/cluster/snapshots/create/CreateSnapshotRequest.java#L94), [body options parsing](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/action/admin/cluster/snapshots/create/CreateSnapshotRequest.java#L473), [closed-index test](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/internalClusterTest/java/org/opensearch/snapshots/SharedClusterSnapshotRestoreIT.java#L894-L917).

A closed index still needs a recoverable, available primary for a full snapshot; listing its name does not repair missing shards. Use `partial: false` and inspect the returned complete index set, `state: SUCCESS`, failed-shard count, and shard totals. Do not close an index while it is being snapshotted: the tagged state service explicitly rejects that operation. [Snapshot primary validation](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/snapshots/SnapshotsService.java#L462-L483), [close/snapshot conflict check](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/cluster/metadata/MetadataIndexStateService.java#L338-L345).

## Independent full snapshot configuration

The exact flags are `remote_store_index_shallow_copy` and `shallow_snapshot_v2`, both repository settings. There is no `shallow_copy` setting in the inspected definition. Keep both false from the creation of a dedicated backup repository; do not repurpose a repository that has already held shallow snapshots. [Tagged flag definitions](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/repositories/blobstore/BlobStoreRepository.java#L415-L417), [shallow snapshot considerations](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/remote-store/snapshot-interoperability/).

```http
PUT /_snapshot/independent-backups
{
  "type": "s3",
  "settings": {
    "bucket": "<backup-bucket>",
    "base_path": "<unique-run>/full-snapshots",
    "region": "us-east-1",
    "remote_store_index_shallow_copy": false,
    "shallow_snapshot_v2": false
  }
}

PUT /_snapshot/independent-backups/before-disaster?wait_for_completion=true
{
  "indices": ["namespace-a", "namespace-b", "namespace-c"],
  "ignore_unavailable": false,
  "include_global_state": true,
  "partial": false
}
```

The ordinary full-snapshot branch acquires a Lucene commit and calls `repository.snapshotShard`, including for remote-store-backed indexes. Shallow snapshots take a different branch that stores remote references. The tagged tests explicitly create full snapshots on remote-store indexes, including a test where shallow-v2 is true but the shallow-copy flag is false. [Full versus shallow creation](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/snapshots/SnapshotShardsService.java#L353-L485), [full-snapshot test](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/internalClusterTest/java/org/opensearch/remotestore/RemoteRestoreSnapshotIT.java#L1403-L1440).

Here, independent means independent of the **source live remote repositories**. Ordinary snapshots are still incremental within their own backup repository and share files with other snapshots there. A prefix in the same bucket also does not provide an independent bucket or account failure domain. [Snapshot repository semantics](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/snapshots/snapshot-restore/).

## Recovery procedures and constraints

### A. Current-state remote recovery after complete local loss

1. Stop the source writer. Retain the live remote segment, translog, and state objects.
2. Start a replacement with an empty local data path, the same `cluster.name`, the same three remote repository locations, and remote state enabled.
3. Verify that index mappings, settings, aliases, and namespace indexes were restored without an external index catalog. A new cluster UUID is expected.
4. For unassigned shards, invoke the remote restore API and wait for shard recovery before querying:

```http
POST /_remotestore/_restore
{"indices": ["namespace-a", "namespace-b", "namespace-c"]}
```

The startup implementation looks up the prior remote state by cluster name only when local metadata has the unknown UUID `_na_`. Reusing a data path containing an already bootstrapped UUID exercises a different path and does not prove remote-state bootstrap. No valid remote state means startup can proceed with empty metadata. [Bootstrap implementation](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/gateway/GatewayMetaState.java#L163-L187), [committed state-chain selection](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/gateway/remote/RemoteClusterStateService.java#L1941-L1958), [validity criterion](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/gateway/remote/RemoteClusterStateService.java#L2091-L2093).

For an existing index, `restore_all_shards=true` requires the index to be closed; this restore reopens it. A normal request repairs unassigned shards. The API relies on existing/restored index metadata and skips nonexistent indexes, so raw remote segments alone are insufficient to reconstruct the complete namespace catalog. Closed indexes recovered from remote cluster state should be opened and their shard state inspected before selecting the appropriate restore request. [Remote restore validation and execution](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/index/recovery/RemoteStoreRestoreService.java#L136-L275).

Remote-state recovery must replace and reseed the cluster-manager nodes after permanent majority loss; the official documentation prohibits unsafe-bootstrap scripts when remote state is enabled. This is recovery of an existing logical cluster lineage, not permission for simultaneous clusters to write the same remote-state lineage. [Remote-state limitation](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/remote-store/remote-cluster-state/#limitations).

### B. Snapshot-only recovery into fresh target storage

Start a fresh target with its own cluster name, local data, and remote segment/translog/state locations. Register only the full backup repository as read-only, using both full-snapshot flags as false, then restore:

```http
POST /_snapshot/independent-backups/before-disaster/_restore?wait_for_completion=true
{
  "indices": "namespace-a,namespace-b,namespace-c",
  "include_global_state": false,
  "include_aliases": true,
  "partial": false,
  "index_settings": {"index.number_of_replicas": 0}
}
```

Index metadata is restored with selected indexes even when global state is false. Restore global state separately only if required and compatible; it can replace persistent settings and templates. Repository registrations are not part of a full snapshot, so the backup repository must be registered on the target. Only one cluster should write a shared snapshot repository. [Create Snapshot API](https://docs.opensearch.org/latest/api-reference/snapshots/create-snapshot/), [Restore Snapshot API](https://docs.opensearch.org/latest/api-reference/snapshots/restore-snapshot/), [repository sharing](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/snapshots/snapshot-restore/#sharing-a-repository-between-clusters).

Do not pass `index.remote_store.enabled`, `index.remote_store.segment.repository`, or `index.remote_store.translog.repository` through user `index_settings` or explicit `ignore_index_settings`: those settings are protected and the request is rejected. The restore service internally substitutes target remote repository settings. On a target without remote **data** attributes, it internally removes `index.remote_store.*`; a remote-state-only target is included in this case. [Protected settings](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/snapshots/RestoreService.java#L160-L174), [non-remote target stripping](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/snapshots/RestoreService.java#L249-L267), [target override](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/snapshots/RestoreService.java#L784-L816), [user-setting rejection](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/snapshots/RestoreService.java#L911-L958).

**Documentation scope discrepancy:** the current general cross-remote-cluster guide broadly requires `source_remote_store_repository` and `source_remote_translog_repository`. Tagged code routes full snapshots to ordinary repository recovery; only shallow snapshots use the source remote-store recovery path, and shallow-v2 also replays source remote translogs. Therefore the source-backed procedure must not be used as evidence that a full snapshot is independent. The decisive experiment must remove source-live-repository access and prove restoration from the full snapshot into fresh target repositories without those source parameters. [Broad documentation](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/snapshots/snapshot-restore/#restoring-snapshots-across-remote-backed-clusters), [recovery dispatch](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/index/shard/IndexShard.java#L4706-L4728), [ordinary snapshot restore](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/index/shard/StoreRecovery.java#L894-L961), [shallow source repository use](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/index/shard/StoreRecovery.java#L382-L455), [shallow-v2 source repositories](https://github.com/opensearch-project/OpenSearch/blob/3.9.0/server/src/main/java/org/opensearch/index/shard/StoreRecovery.java#L499-L536).

Other restore constraints: an existing same-name target index must be closed or deleted, or the restore must rename it. Restoring over a closed index requires the same primary-shard count and reopens it. Do not use `storage_type: remote_snapshot`, which is searchable-snapshot behavior and has a warm-node requirement. Use the same OpenSearch version for this compatibility experiment, remove incompatible allocation filters if any, and inspect recovery completion rather than treating an accepted restore request as success. [Restore Snapshot API](https://docs.opensearch.org/latest/api-reference/snapshots/restore-snapshot/).

## Outstanding runtime evidence

The following remain unproven by this research and must be established from the existing remote experiment or a separately authorized extension of it:

- The running process uses the selected official ARM64 3.9.0 artifact with required plugins and verified identity. If Docker is used, verify the pinned image/configuration digests above rather than a matching tag alone. A native installation needs its own distribution-checksum and runtime-build verification.
- Acknowledged writes that remain unrefreshed and unflushed survive loss of the only local data directory; exact record contents, updates, and deletes match the acknowledgement ledger.
- Closed namespace indexes are included in a successful full snapshot and their content, mappings, aliases, and intended lifecycle state are recovered.
- A new node discovers the complete namespace/index catalog from remote cluster state, with no PostgreSQL query or index list supplied by an external database.
- Full snapshot restoration succeeds when all source live segment/translog/state repositories are unavailable, using only the backup repository and fresh target remote storage.
- Synthetic lexical queries, namespace isolation, and post-recovery writes behave correctly. Revision-bound application results and citations remain later integration checks. Raw document counts alone do not establish either scope.
- Recovery time, memory use after close/open, object-store errors, and operation under resource pressure are measured on the actual host. Official benchmarks do not establish these values for one node.

The published documentation is currently the `latest` site. Version-specific conclusions above are tied to the official `3.9.0` source tag and commit identity. Any mismatch between the official tagged source and the runtime build must be resolved before carrying a source-derived claim into the experiment result.

Subsequent runtime update, 2026-11-03: the [Oracle functional PoC](opensearch-compatibility-2026-11-03.md)
passed seven cases with this pinned artifact and S3 emulation, covering
close/open, closed-index full-snapshot coverage, complete local-data loss,
remote-state/acknowledged-tail recovery, source-independent full restore,
and post-recovery writes. This supplies fixture-level runtime evidence for
those items; actual AWS behavior, production capacity, application contracts,
and end-to-end recovery objectives remain outside that result.
