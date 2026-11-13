# Private OpenSearch installation and operation

This directory contains runnable configuration generators and operator adapters.
No AWS resource, native service, alert notification, staging campaign, or release
has been executed by these development tests. Real recovery, timing, load, and
notification delivery remain target-environment acceptance work.

## Prepare the reviewed installation

Copy `manifest.example.json` to a private location and fill the actual existing
VPC/subnet/AZ, approved ARM64 AMI, API/worker/management security groups, S3 gateway
prefix list, buckets/prefixes, certificate DNs, archive/plugin paths and SHA-512
digests, and reviewed application commit. Empty example values intentionally fail
validation. No bucket, NAT gateway, KMS key, replica, or second always-on node is
created by the generated CloudFormation template. Existing private SSM endpoints,
their endpoint security group, the S3 gateway route/policy, DNS, and encryption
access must be verified independently.

```bash
uv run --offline --frozen python deploy/opensearch/manage.py render \
  --manifest /private/knowhere-native/installation.json \
  --directory /private/knowhere-native/reviewed-bundle

uv run --offline --frozen python deploy/opensearch/manage.py preflight \
  --manifest /private/knowhere-native/installation.json \
  --proofs /private/knowhere-native/actual-preflight-receipts.json \
  --output /private/knowhere-native/preflight.json

uv run --offline --frozen python deploy/opensearch/manage.py cost \
  --quote /private/knowhere-native/current-quote.json \
  --usage /private/knowhere-native/bounded-usage.json \
  --output /private/knowhere-native/cost-plan.json
```

`operations.py` defines the exact quote/usage schema. Include 730 compute hours,
120 GiB gp3, actual remote/snapshot storage and PUT/LIST/GET/network/existing-KMS
usage, and separately bounded temporary recovery compute, extra EBS, S3 and
requests. The generator supplies no invented prices or request-volume estimates.
Keep current quote sources/dates, actual account/network/bucket policies, resource
identifiers and the campaign's maximum approved spend with the private manifest.

The bundle fixes one `r7g.xlarge`, a 16-GiB heap, encrypted retained 100-GiB gp3
data volume (3,000 IOPS/125 MiB/s), and a 20-GiB encrypted system volume. It enables
remote segments/translogs/state, request-durable translogs, mandatory REST/transport
TLS and client certificate authentication. Repository S3 credentials come from
the node's scoped instance role. Application clients receive separate certificate
identities; they receive no snapshot/restore/node-administration role.

CloudFormation execution is an explicit approved operator action outside this
tool. Review a change set from `cloudformation.json` and preserve its resource
receipts. The template's retained volume continues to exist after stack removal.

## Install and authenticate

Run `install` on the approved ARM64 host. Its default prints the plan without
root, artifact access, mounting, or service changes:

```bash
uv run --offline --frozen python deploy/opensearch/manage.py install \
  --bundle /private/knowhere-native/reviewed-bundle \
  --data-device /dev/disk/by-id/APPROVED_EBS_DEVICE \
  --data-volume-id vol-APPROVED \
  --tls-directory /run/secrets/knowhere-native-node
```

For approved execution add `--execute --approval /private/approval.json` and a new
`--output` receipt. The installer verifies artifact digests and device serial,
mounts an existing ext4/XFS filesystem, installs a persistent UUID mount unit,
installs the pinned repository plugin, and enables systemd supervision. It never
formats a device or starts the service. An existing binary with another digest
requires a separately reviewed upgrade. Copy the reviewed operational config and
preserve its receipt outside the data disk.

The approval JSON must bind `manifest_sha256`, `approved:true`, an actual
`user_approval_reference`, `bounded_cost_plan_sha256`, timezone-qualified
`expires_at`, and approved `purposes` (`install`, `bootstrap`, `backup`, `recovery`,
or `release`). This is a record of prior explicit user approval; generating a
template or editing an approval file does not itself grant approval.

Use the node/admin certificate material to start and initialize the Security
plugin through the printed `securityadmin.sh` command. Keep the admin key outside
application/node runtime config. Then review/run `manage.py bootstrap --manifest
...`, adding the approved receipt and `--execute` only for actual initialization.
It registers the independent full repository, verifies all live/state/full
repositories, and installs the request-durability index template.

For both worker and API clients, mount these exact environment/file bindings:

```text
KNOWHERE_OPENSEARCH_URL=https://APPROVED_PRIVATE_DNS:9200
KNOWHERE_OPENSEARCH_CA_FILE=/run/secrets/knowhere-native/ca.pem
KNOWHERE_OPENSEARCH_CLIENT_CERT_FILE=/run/secrets/knowhere-native/client.pem
KNOWHERE_OPENSEARCH_CLIENT_KEY_FILE=/run/secrets/knowhere-native/client-key.pem
```

Basic authentication is alternatively supported with
`KNOWHERE_OPENSEARCH_USERNAME` and `KNOWHERE_OPENSEARCH_PASSWORD_FILE` against a
separately reviewed node authentication configuration. The generated node bundle
uses mutual TLS. Private keys/password files must be bounded regular files with
no group/other permissions; preserve hostname verification and explicit CA trust.
Non-loopback HTTP and public/metadata IPs are rejected. URL credentials are
rejected. The two client runtimes retain at most two credential generations per
endpoint/process or API event loop, avoid joining an active old pool, and require
a coordinated restart before a third generation. Rehearse rotation on actual
authenticated clients; the local SSL-context tests are development evidence.

## Host monitoring

`host-monitoring-stack.yaml` is deployed as its own stack,
`knowhere-opensearch-monitoring`, so monitoring changes never update the host
stack. It grants the host role permission to publish only to the `CWAgent`
namespace and stores the CloudWatch agent configuration in SSM Parameter Store.
The agent reports the data-volume usage and the OpenSearch process count every
minute. Four alarms feed one composite alarm,
`knowhere-opensearch-action-required`, which notifies the existing alert topic on
both alarm and recovery:

| Alarm | Condition |
| --- | --- |
| `knowhere-opensearch-status-check-failed` | EC2 status check fails for 3 consecutive minutes. |
| `knowhere-opensearch-process-down` | No OpenSearch process for 3 consecutive minutes. Missing data counts as down. |
| `knowhere-opensearch-data-disk-high` | Data volume at or above 75% for 15 minutes. |
| `knowhere-opensearch-cpu-high` | Host CPU averages 85% or more for 15 minutes. |

Deploy changes through a reviewed change set:

```bash
aws cloudformation create-change-set --stack-name knowhere-opensearch-monitoring \
  --change-set-name <name> --capabilities CAPABILITY_IAM \
  --template-body file://deploy/opensearch/host-monitoring-stack.yaml \
  --parameters ParameterKey=HostRoleName,ParameterValue=<host role> \
               ParameterKey=HostInstanceId,ParameterValue=<instance id>
```

Install the agent once with the `AWS-ConfigureAWSPackage` SSM document
(`AmazonCloudWatchAgent`), then load the configuration with
`AmazonCloudWatch-ManageAgent` using `optionalConfigurationSource=ssm` and the
parameter name from the stack. A replacement host needs both steps again and a
stack update with its instance ID. These alarms watch the host and process,
not search correctness.

## Hourly independent full backups

Install the generated backup and monitor services and timers on the existing
operational host, separate from the native node and parsing workers. Use an
immutable reviewed checkout, private `knowhere-ops` user/state directories,
and a root-owned mode-0600 `/etc/knowhere-native-ops/environment`. Install
`backup-cycle.sh` and `monitor-check.sh` under that directory and the systemd units
under `/etc/systemd/system`. The timer starts a bounded transient system service
with a fresh physical token on each due check. Its control group is fenceable by
the existing SSM operator. It polls every minute and attempts the next snapshot
after an hour from the previous submission, subject to prior operation completion
and reviewed target availability. A long-running or incomplete backup can delay
the next submission; the monitor runs independently.

The generated scripts list the exact `KNOWHERE_OPS_*` paths, Python interpreter,
frozen epoch/source, stable backup workflow UUID, and monitor freshness and repeat
intervals. Mount source `KNOWHERE_OPENSEARCH_*` and separate offline
`KNOWHERE_OFFLINE_OPENSEARCH_*` CA and client credential files. Use the existing
approved Logfire token. The monitor reads authoritative publication/PG-copy ages
and the backup journal, and checks native health separately. The resource pressure
file supplies separately reviewed thresholds/measurements. Logging is not proof
that a notification was delivered.

The backup driver exports the complete OPEN/CLOSED catalog and current published
document, revision, build, update and delete boundary from PostgreSQL under the
maintenance role. Its receipt binds compact per-build unit/term fingerprints,
without exporting tokens. The source native identity and catalog are read from
the approved endpoint and compared before submission. CLOSED shard counts come
from index metadata, so missing CAT health cannot silently drop a closed index.
The full snapshot explicitly includes managed names with `expand_wildcards:"all"`
and excludes global/security state. It does not open the source catalog or hold
an exclusive catalog lock across the native request.

Use the independent restore bundle on the separately approved offline target.
Its template permits management ingress only, disables live remote state, and
uses read-only full-repository access with a deny policy on the live prefixes.
It is a temporary target within the approved cost window, with no second
always-on serving node. Install and bootstrap with `--full-restore`. The target
must have its own TLS service identity. Its reviewed target plan identifies the
actual existing role session, cluster/incarnation, old source UUID, three live
prefixes and one known existing sentinel key per prefix. To check actual denial
on that offline node before using it:

```bash
uv run --offline --frozen python deploy/opensearch/prove-offline-denial.py \
  --manifest /private/knowhere-native/installation.json \
  --plan /private/knowhere-native/offline-denial-plan.json \
  --approval /private/knowhere-native/offline-denial-approval.json \
  --output /private/knowhere-native/actual-offline-denial.json
```

Dry-run performs no network I/O. Approved `--execute` makes six bounded S3 read
checks using the offline EC2 instance profile. It records actual denied access
and the target identity without recording object keys. The backup plan binds
this exact private file by path and SHA, then independently checks the full
snapshot and actual restored payloads.

The pinned 3.9.0 upstream
[CLOSED full FS snapshot integration test](https://github.com/opensearch-project/OpenSearch/blob/4ee42a94e87f66fbf1e62a9871b1b87f91e02472/server/src/internalClusterTest/java/org/opensearch/snapshots/SharedClusterSnapshotRestoreIT.java#L894-L923)
closes one index, snapshots both names with closed expansion, and asserts complete
coverage without shard failures. To collect local component evidence, an operator
can run the two-index probe against the serially owned loopback node after its
existing `path.repo` is configured:

```bash
uv run --offline --frozen python scripts/probes/verify-opensearch-closed-snapshot.py \
  --endpoint http://127.0.0.1:OWNED_PORT \
  --repository-root /tmp/knowhere-kw01-backups \
  --receipt /private/knowhere-native/closed-full-copy.json
```

Add `--execute` only for that owned component probe. It retains two unique restored
synthetic indices and one FS snapshot, checks the source stayed CLOSED during full
capture, deletes only its two source indices, and verifies restored document
content. It performs no AWS operation and does not qualify independent S3 recovery.

```bash
uv run --offline --frozen python deploy/opensearch/launch-issuer.py \
  --kind operator --environment-file /etc/knowhere-native-ops/environment \
  --maximum-seconds 3600 -- backup \
  --manifest /private/knowhere-native/installation.json \
  --plan /private/knowhere-native/backup-plan.json \
  --journal /private/knowhere-native/backup-journal.json \
  --output /private/knowhere-native/new-backup-receipt.json \
  --operation-id APPROVED_BACKUP_WORKFLOW_UUID --epoch 42 \
  --source-hash REVIEWED_AGGREGATE_SOURCE_HASH \
  --operation-state-directory /var/lib/knowhere-native-ops \
  --approval /private/knowhere-native/backup-approval.json
```

Add `--execute` before `-- backup` only after approval. The backup plan binds
`source_incarnation`, `target_endpoint`, `target_cluster_uuid`,
`target_incarnation`, `offline_target_nonserving:true`,
`live_repository_denial_receipt_path` and `live_repository_denial_receipt_sha256`,
`batch_limit`, `snapshot_poll_seconds`, `restore_timeout_seconds` and
`cleanup_verified_clones:true`. Review the operator's own dry-run for its
canonical plan SHA; the approval additionally binds that SHA, aggregate source,
actual host fingerprint and canonical operation-state directory. Keep the same
workflow UUID and journal for recurring submissions; physical issuer UUIDs are
always one-use. Use a new output receipt on each run.

Before each restore batch, PostgreSQL admits at most 400 combined open,
reserved and uncertain target indices. The verifier reads actual restored units
and checks their frozen fingerprints. Close must acknowledge the exact verified
clone, then source admission observes CLOSED before releasing its slot. Cleanup
deletes only exact owned, verified CLOSED clones after sealing their proof.
An unknown restore/close/delete is retained in the private journal; do not
replay it or reuse that target identity until reconciliation. Historical proof
and any new current-build coverage gap are separate.

The source journal records exact catalog/UUID and per-primary DONE evidence,
snapshot start/end/duration, data boundary and independent verification.
Retention protects the newest three verified points even beyond seven days,
rechecks their actual UUIDs before each native snapshot delete, and uses no
object TTL. File-only `manage.py snapshot-verify` is catalog review; a
caller-provided digest cannot satisfy the restored payload proof or retention floor.

## Manual native recovery

Use the source-owned transition CLI with an explicitly authorized maintenance DB
role and exact epoch to gate/settle application publication. Fence the old native
issuer and preserve unresolved operations. Check actual AWS/process state before
attaching a volume or starting a replacement; marking an application job failed
does not prove that a native issuer stopped.

- Process or healthy volume: use the existing-host driver with the approved AWS
  account, original operator/native and replacement instance IDs, same AZ,
  EBS volume and filesystem UUID, and pinned installed-receipt SHA. Process
  recovery uses the same instance and never moves the volume. Healthy-volume
  recovery observes the old instance stopped before non-forced detach, waits
  for attach on the approved target, verifies filesystem and mount identity,
  and starts the pinned service. It never formats or provisions.
- Unresolved prior physical issuer: execute `operator.py fence-host` in its own
  physical unit. The plan binds the exact actual `old_issuer_receipt_path` and
  raw `old_issuer_receipt_sha256`, plus the old issuer identity fields. The
  source gate reserves one durable fencing intent before the SSM sends. It
  retains the actual command IDs and confirms the exact operator control group
  and native unit are stopped. If an acknowledgment is lost,
  `operator.py reconcile-fence --fence-token ORIGINAL_UUID` observes existing
  tagged commands only; it never reissues them. Takeover requires the proven
  receipt and exact old-token CAS. No TTL frees unknown authority.
- An interrupted first shadow pilot while PostgreSQL is serving can use
  `operator.py release-empty-shadow` with the same old-issuer plan. The command
  observes the collected transient unit through SSM and checks the exact native
  identity, namespace inventory, and primary shard history. It records the
  release only before any migration attempt or native write. The shared native
  service stays online. Run the selected inputs through the read-only
  preflight under the pilot's temporary database role before retrying.
- After a successful host restart, execute the source-owned
  `operator.py reconcile-restart` against the same recovery operation UUID in
  a new physical invocation. The plan binds `inventory_path`, `inventory_sha256`,
  `host_recovery_receipt_path`, `transition_script_sha256`, `target_backend`
  and `maximum_step_seconds`. The restart inventory v1 contains
  `cluster_uuid`, `old_incarnation`, `new_incarnation` and
  `host_recovery_receipt_sha256`. It confirms same native UUID/new incarnation
  against actual metadata at the already advanced gated epoch, keeps uncertain
  slots and exact operation IDs, and performs no native mutation. Coverage
  verification and private/public API smoke still determine service restoration.
- Lost local data: bootstrap the approved live remote-state configuration on
  approved replacement infrastructure. Confirm the restored existing metadata,
  then use `live_remote` admission below for unassigned primaries or closed
  indexes. An empty running cluster is not recovered service.
- Independent full snapshot: use the separately approved readonly repository
  and verified data boundary, then the snapshot route below. The independent
  nonremote verifier establishes historical data recovery, not serving readiness
  or restored durability. Newer published data can remain unavailable.

Use the `launch-issuer.py --kind operator` form above with `fence-host`,
`reconcile-fence`, `release-empty-shadow`, `recover-process`, `recover-volume`,
or `reconcile-restart`.
For a takeover supply `--expected-issuer-token OLD_UUID --fence-receipt
/private/actual-fence.json` to the child command. Never start both old and new
native hosts against the retained data.

The source-owned restore inventory v1 is defined by
`shared/services/retrieval/lexical/restore_admission.py`. Snapshot plans bind the
approved snapshot UUID/manifest and exact old-to-new index UUID mapping. Fresh
snapshot restoration creates new physical index UUIDs; capture-time UUID stability
does not mean a restored index keeps its old UUID. `live_remote` plans instead bind
the reviewed remote-state evidence and existing target metadata, including
namespaces created after the last snapshot. No snapshot catalog is required there.

```bash
uv run --offline --frozen python deploy/opensearch/recovery.py submit \
  --manifest /private/knowhere-native/installation.json \
  --inventory /private/knowhere-native/restore-batch.json \
  --epoch 42 --operation-id APPROVED_OPERATION_UUID \
  --journal /private/knowhere-native/recovery/batch-journal.json \
  --receipt-directory /private/knowhere-native/recovery/receipts \
  --transition-source-hash APPROVED_SOURCE_HASH \
  --transition-script-sha256 APPROVED_COMMAND_HASH
```

The default prints a plan. Approved execution uses `launch-issuer.py --kind recovery`
with the same child arguments and an approval
whose `restore_inventory_sha256` binds the exact reviewed plan. Admission happens
before native restore and checks the authoritative charged union against 400.
Already occupied native/registry slots are counted once. For live remote recovery,
the adapter uses each reserve-phase ID as `X-Opaque-Id`, selecting all-shard restore
for a closed index and unassigned-shard restore for an open index. Async acceptance
is recorded; it cannot replace READY/unit coverage verification.

Run `recovery.py finish` with the same operation/epoch/journal, the actual UUID/
remote-operation attestation inventory, and `--reservation` pointing to the original
reserve receipt. Source-owned verification checks actual per-build coverage and
adopts only the reviewed identities. It installs a new charged close fence. The
adapter closes under that verified reservation ID, records only acknowledged exact
closure, and releases after source-owned live CLOSED/identity reconciliation.
Unknown restore/close outcomes retain charges; do not repeat native operations or
restore the next batch until issuer/lifecycle uncertainty is reconciled.
The adapter verifies its own HTTP target cluster UUID and single-node incarnation
before each native mutation. Recovery and release also pass the reviewed aggregate
`--expected-source-hash` into the transition process, which checks it before any
session/admission mutation. A matching script checksum alone is insufficient.

Reserve each bounded batch before restore, count current open/reserved/uncertain
state, and verify/close/reconcile it before the next batch. Do not mass-restore a
catalog and enforce capacity afterward. Required current published builds and API
hydration must pass before a separately controlled traffic reopen. Missing newer
builds remain unavailable/escalated; no PG regeneration or pointer rewind exists.
Measure original-disruption-to-serving time, including detection, human response
and failed earlier attempts, against the 15-minute healthy-volume and one-hour
lost-volume goals. Historical snapshot restoration is a separate result.

## Two-release rehearsal and cutover

Release 1 keeps PostgreSQL primary, deploys compatible API and worker artifacts,
drains old ECS tasks, and performs bounded native shadow backfill and verification.
Release 2 executes the source-owned gate/settle/backfill/verify/resume sequence
under the original traffic disruption clock. The existing compatible PostgreSQL
rollback uses its own reviewed task definitions and source coverage verification.
It preserves additive schema, parsed results, charges and logical events.

Use `launch-issuer.py --kind operator` with the `release` child command shown
by `operator.py --help`. Review separate plans for `phase:"prepare"`,
`"forward"` and `"rollback"`. Each plan binds `account_id`, exact source hash,
transition script SHA, existing gate rule ARN, conditions SHA, original forward
actions and a known fixed 503 response. The tool checks the actual route
closure using the reviewed public probe path before any source transition. Each
deployment record binds an existing ECS cluster/service, role (`api` or
`worker`), container, expected prior and target task-definition ARNs, immutable
image digest and reviewed task environment. Register and review the existing
task definitions via the repository's approved ECS release process first.

Bind private/public HTTPS origins and smoke lists. Each list needs actual phase
and retrieval/citation evidence cases. Read-only smoke runs behind the closed
gate after matching runtime and SQL selection, then again through the customer
route after reopening. The tool verifies the gate's current ownership before
restoring its original action. An unknown cloud/native action preserves the
journal and prevents an opposite action. A known failure allows an explicit
compatible rollback with the original operation, gate, first impact and exact
`forward_plan_sha256`. Use the reviewed `maximum_workflow_seconds`,
`maximum_step_seconds`, batch bounds and positive margins.

The release qualification plan references each actual private receipt by
`path` and raw `sha256`. The restoration receipt binds actual first customer
impact, full current-published coverage and canonical API smoke; include
detection, manual response and failed preceding attempts. The production
abort point is `T0 + 2 hours - measured restoration - explicit margin`.
A first isolated staging rehearsal can use `rehearsal:true` and a provisional
budget with an actual reviewed `staging-rehearsal-isolation` receipt. The forward
stage deliberately retains the closed gate for explicit compatible rollback,
preserving T0. Its measured outcome still needs review before becoming a
production restoration receipt.

`release.py` remains available for individual reviewed steps under
`launch-issuer.py --kind release-step`. It never manages customer traffic. The
actual rule, task definitions, credentials, security scopes and approval must
be supplied for the target environment; no development test changes them.

## Development validation

```bash
uv run --offline --frozen python -m pytest -q deploy/opensearch/tests \
  scripts/probes/tests/test_opensearch_client_security.py
uv run --offline --frozen ruff check deploy/opensearch/*.py \
  packages/shared-python/shared/services/retrieval/lexical/clientconfig.py
uv run --offline --frozen pyright deploy/opensearch/*.py \
  packages/shared-python/shared/services/retrieval/lexical/clientconfig.py
```

Official source pointers and the verified 3.9.0 source commit are recorded in
`official-sources.json`. The tests use pure decisions or injected HTTP transports;
they do not prove AWS permissions/encryption recovery, live TLS authorization,
snapshot independence at production size, alert delivery, RTO or a release window.
