"""Deterministic deployment, backup, recovery, alert and release decisions.

No function in this module calls AWS, starts a service, sends a notification or
changes application state. The command adapter records and executes separately
approved operations. Native payloads follow the official 3.9.0 candidate and its
versioned source, not managed-service IAM authentication assumptions.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

__all__: list[str] = ["OpenSearchOperations"]
_DAY_MILLISECONDS: int = 86_400_000


class _Installation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["opensearch-installation-v1"]
    environment: Literal["staging", "production"]
    region: Literal["us-east-1"]
    vpc_id: str = Field(pattern="^vpc-[a-f0-9]{8,17}$")
    subnet_id: str = Field(pattern="^subnet-[a-f0-9]{8,17}$")
    availability_zone: str = Field(pattern="^us-east-1[a-z]$")
    ami_id: str = Field(pattern="^ami-[a-f0-9]{8,17}$")
    api_security_group: str = Field(pattern="^sg-[a-f0-9]{8,17}$")
    worker_security_group: str = Field(pattern="^sg-[a-f0-9]{8,17}$")
    management_security_group: str = Field(pattern="^sg-[a-f0-9]{8,17}$")
    s3_prefix_list: str = Field(pattern="^pl-[a-f0-9]{8,17}$")
    cluster_name: str = Field(pattern="^[a-z][a-z0-9-]{0,62}$")
    endpoint_host: str = Field(pattern="^[a-z0-9][a-z0-9.-]+$")
    index_prefix: str = Field(pattern="^[a-z][a-z0-9-]{0,39}$")
    node_dn: str = Field(min_length=4)
    admin_dn: str = Field(min_length=4)
    api_dn: str = Field(min_length=4)
    worker_dn: str = Field(min_length=4)
    backup_dn: str = Field(min_length=4)
    monitor_dn: str = Field(min_length=4)
    live_bucket: str = Field(pattern="^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    backup_bucket: str = Field(pattern="^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    segment_prefix: str = Field(pattern="^[a-z0-9][a-z0-9/_-]+$")
    translog_prefix: str = Field(pattern="^[a-z0-9][a-z0-9/_-]+$")
    state_prefix: str = Field(pattern="^[a-z0-9][a-z0-9/_-]+$")
    snapshot_prefix: str = Field(pattern="^[a-z0-9][a-z0-9/_-]+$")
    archive_path: str = Field(pattern="^/")
    archive_sha512: str = Field(pattern="^[a-f0-9]{128}$")
    plugin_path: str = Field(pattern="^/")
    plugin_sha512: str = Field(pattern="^[a-f0-9]{128}$")
    source_revision: str = Field(pattern="^[a-f0-9]{40}$")
    existing_kms_key_arn: str | None = Field(default=None, pattern="^arn:aws:kms:us-east-1:[0-9]{12}:key/[a-f0-9-]+$")

    @model_validator(mode="after")
    def validateRepositoryIsolation(self) -> Self:
        repositories: list[tuple[str, str]] = [
            (self.live_bucket, self.segment_prefix.rstrip("/")),
            (self.live_bucket, self.translog_prefix.rstrip("/")),
            (self.live_bucket, self.state_prefix.rstrip("/")),
            (self.backup_bucket, self.snapshot_prefix.rstrip("/")),
        ]
        for index, (bucket, prefix) in enumerate(repositories):
            for otherBucket, otherPrefix in repositories[index + 1:]:
                if bucket == otherBucket and (prefix == otherPrefix or prefix.startswith(otherPrefix + "/") or otherPrefix.startswith(prefix + "/")):
                    raise ValueError("Live/state/full-snapshot repository prefixes must not overlap")
        return self


class _Index(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern="^[a-z0-9][a-z0-9_.-]+$")
    uuid: str = Field(min_length=1)
    state: Literal["open", "closed"]
    primary_shards: int = Field(ge=1)


class _Point(BaseModel):
    snapshot: str = Field(min_length=1)
    state: Literal["SUCCESS", "FAILED", "PARTIAL", "IN_PROGRESS"]
    coverage_verified: bool
    completed_at_millis: int = Field(ge=0)


class _Fence(BaseModel):
    old_issuer_stopped: bool
    application_gated: bool
    native_writers_settled: bool
    old_epoch: int = Field(ge=0)
    new_epoch: int = Field(ge=1)
    proof_sha256: str = Field(pattern="^[a-f0-9]{64}$")


class _Shard(BaseModel):
    stage: str


class _IndexStatus(BaseModel):
    shards: dict[str, _Shard]


class _SnapshotStatus(BaseModel):
    snapshot: str
    uuid: str
    state: str
    indices: dict[str, _IndexStatus]


class _Statuses(BaseModel):
    snapshots: list[_SnapshotStatus] = Field(min_length=1, max_length=1)


class _Metrics(BaseModel):
    native_available: bool
    indexing_oldest_seconds: float = Field(ge=0)
    pg_copy_oldest_seconds: float = Field(ge=0)
    latest_complete_snapshot_age_seconds: float | None = Field(ge=0)
    backup_incomplete: bool
    retrieval_unavailable: bool = False
    final_indexing_failure: bool = False
    pg_copy_final_failure: bool = False
    identity_failure: bool = False
    # Additional qualified numeric resource rules are supplied by the operator.
    resource_pressure: dict[str, bool] = Field(default_factory=dict)


class _AlertState(BaseModel):
    active: bool
    last_emitted_seconds: int


class _Quote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    as_of: str = Field(pattern="^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    source: str = Field(pattern="^https://")
    compute_hour: float = Field(ge=0)
    gp3_gib_month: float = Field(ge=0)
    s3_gib_month: float = Field(ge=0)
    put_per_thousand: float = Field(ge=0)
    get_per_thousand: float = Field(ge=0)
    network_gib: float = Field(ge=0)
    kms_per_ten_thousand: float = Field(ge=0)


class _Usage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    remote_gib: float = Field(ge=0)
    snapshot_gib: float = Field(ge=0)
    put_requests: int = Field(ge=0)
    get_requests: int = Field(ge=0)
    network_gib: float = Field(ge=0)
    kms_requests: int = Field(ge=0)
    temporary_recovery_hours: float = Field(gt=0)
    temporary_recovery_extra_gib: float = Field(ge=0)
    temporary_recovery_s3_gib_month_equivalent: float = Field(ge=0)
    temporary_recovery_put_requests: int = Field(ge=0)
    temporary_recovery_get_requests: int = Field(ge=0)
    temporary_recovery_network_gib: float = Field(ge=0)
    temporary_recovery_kms_requests: int = Field(ge=0)


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validateInventory(value: object) -> list[_Index]:
    if not isinstance(value, list) or not value:
        raise ValueError("Expected the complete nonempty open and closed native inventory")
    indexes: list[_Index] = [_Index.model_validate(item) for item in value]
    if len({index.name for index in indexes}) != len(indexes):
        raise ValueError("Native inventory contains duplicate index names")
    return indexes


class OpenSearchOperations:
    """Keep safety decisions independent of cloud and native I/O adapters."""

    @staticmethod
    def renderBundle(value: object) -> dict[str, object]:
        installation: _Installation = _Installation.model_validate(value)
        configuration: dict[str, object] = {
            "cluster.name": installation.cluster_name,
            "node.name": "native-1", "network.host": "0.0.0.0",
            "discovery.type": "single-node", "path.data": "/var/lib/knowhere-opensearch",
            "path.logs": "/var/log/knowhere-opensearch", "bootstrap.memory_lock": True,
            "cluster.remote_store.state.enabled": True,
            "cluster.remote_store.index.restrict.async-durability": True,
            "plugins.security.ssl.transport.pemcert_filepath": "tls/node.pem",
            "plugins.security.ssl.transport.pemkey_filepath": "tls/node-key.pem",
            "plugins.security.ssl.transport.pemtrustedcas_filepath": "tls/ca.pem",
            "plugins.security.ssl.transport.enforce_hostname_verification": True,
            "plugins.security.ssl.http.enabled": True,
            "plugins.security.ssl.http.pemcert_filepath": "tls/node.pem",
            "plugins.security.ssl.http.pemkey_filepath": "tls/node-key.pem",
            "plugins.security.ssl.http.pemtrustedcas_filepath": "tls/ca.pem",
            "plugins.security.ssl.http.clientauth_mode": "REQUIRE",
            "plugins.security.nodes_dn": [installation.node_dn],
            "plugins.security.authcz.admin_dn": [installation.admin_dn],
            "plugins.security.allow_default_init_securityindex": False,
        }
        for kind, prefix in (("segment", installation.segment_prefix), ("translog", installation.translog_prefix), ("state", installation.state_prefix)):
            name: str = {"segment": "knowhere-segments", "translog": "knowhere-translogs", "state": "knowhere-state"}[kind]
            configuration[f"node.attr.remote_store.{kind}.repository"] = name
            base: str = f"node.attr.remote_store.repository.{name}"
            configuration.update({f"{base}.type": "s3", f"{base}.settings.bucket": installation.live_bucket,
                f"{base}.settings.base_path": prefix, f"{base}.settings.region": installation.region})
        service: str = "\n".join([
            "[Unit]", "Description=Knowhere private OpenSearch 3.9.0", "After=network-online.target",
            "Wants=network-online.target", "RequiresMountsFor=/var/lib/knowhere-opensearch", "",
            "[Service]", "Type=simple", "User=opensearch", "Group=opensearch",
            'Environment="OPENSEARCH_JAVA_OPTS=-Xms16g -Xmx16g"',
            "Environment=OPENSEARCH_PATH_CONF=/etc/knowhere-opensearch",
            "ExecStart=/opt/knowhere-opensearch/bin/opensearch", "Restart=on-failure", "RestartSec=5",
            "LimitNOFILE=65535", "LimitMEMLOCK=infinity", "TimeoutStopSec=120", "UMask=0027",
            "NoNewPrivileges=true", "PrivateTmp=true", "ProtectHome=true", "ProtectSystem=full", "",
            "[Install]", "WantedBy=multi-user.target", "",
        ])
        indexSettings: dict[str, object] = {
            "index.number_of_shards": 1, "index.number_of_replicas": 0,
            "index.replication.type": "SEGMENT", "index.translog.durability": "request",
        }
        fullRepository: dict[str, object] = {"type": "s3", "settings": {
            "bucket": installation.backup_bucket, "base_path": installation.snapshot_prefix,
            "region": installation.region, "remote_store_index_shallow_copy": False,
            "shallow_snapshot_v2": False, "readonly": False,
        }}
        independentConfiguration: dict[str, object] = {key: value for key, value in configuration.items()
            if not key.startswith("node.attr.remote_store.")}
        independentConfiguration["cluster.remote_store.state.enabled"] = False
        return {
            "cloudformation.json": OpenSearchOperations._renderInfrastructure(installation),
            "full-restore-cloudformation.json": OpenSearchOperations._renderInfrastructure(installation, isIndependent=True),
            "opensearch.yml": configuration,
            "index-template.json": {"index_patterns": [installation.index_prefix + "-*"], "priority": 500, "template": {"settings": indexSettings}},
            "snapshot-repository.json": fullRepository,
            "full-restore-opensearch.yml": independentConfiguration,
            "full-restore-repository.json": {"type": "s3", "settings": {
                **fullRepository["settings"], "readonly": True,  # type: ignore[dict-item]
            }},
            "deny-live-repositories-for-independent-restore.json": {"Version": "2012-10-17", "Statement": [
                {"Effect": "Deny", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], "Resource": [f"arn:aws:s3:::{installation.live_bucket}/{prefix}/*" for prefix in (installation.segment_prefix, installation.translog_prefix, installation.state_prefix)]},
                {"Effect": "Deny", "Action": ["s3:ListBucket"], "Resource": [f"arn:aws:s3:::{installation.live_bucket}"], "Condition": {"StringLike": {"s3:prefix": [prefix + "*" for prefix in (installation.segment_prefix, installation.translog_prefix, installation.state_prefix)]}}},
            ]},
            "knowhere-opensearch.service": service,
            "knowhere-native-backup.service": "\n".join([
                "[Unit]", "Description=Knowhere hourly full native backup on existing operations host", "After=network-online.target", "",
                "[Service]", "Type=oneshot", "User=root", "EnvironmentFile=/etc/knowhere-native-ops/environment",
                "ExecStart=/etc/knowhere-native-ops/backup-cycle.sh", "TimeoutStartSec=61min", "UMask=0077", "NoNewPrivileges=true", "",
            ]),
            "knowhere-native-backup.timer": "\n".join([
                "[Unit]", "Description=Minute polling for hourly full native snapshots", "",
                "[Timer]", "OnCalendar=*-*-* *:*:00", "Persistent=true", "Unit=knowhere-native-backup.service", "",
                "[Install]", "WantedBy=timers.target", "",
            ]),
            "knowhere-native-monitor.service": "\n".join([
                "[Unit]", "Description=Independent off-node native/age monitor", "After=network-online.target", "",
                "[Service]", "Type=oneshot", "User=knowhere-ops", "EnvironmentFile=/etc/knowhere-native-ops/environment",
                "ExecStart=/etc/knowhere-native-ops/monitor-check.sh",
                "TimeoutStartSec=45s", "UMask=0077", "NoNewPrivileges=true", "",
            ]),
            "knowhere-native-monitor.timer": "\n".join([
                "[Unit]", "Description=Independent off-node native health checks (candidate one-minute cadence)", "",
                "[Timer]", "OnBootSec=60s", "OnUnitActiveSec=60s", "Unit=knowhere-native-monitor.service", "",
                "[Install]", "WantedBy=timers.target", "",
            ]),
            "backup-cycle.sh": "\n".join([
                "#!/usr/bin/env bash", "set -euo pipefail",
                'cd "${KNOWHERE_OPS_REPOSITORY:?}"',
                'exec "${KNOWHERE_OPS_PYTHON:?}" deploy/opensearch/launch-issuer.py --kind operator --environment-file /etc/knowhere-native-ops/environment --maximum-seconds 3600 --execute -- backup --manifest "${KNOWHERE_OPS_MANIFEST:?}" --plan "${KNOWHERE_OPS_BACKUP_PLAN:?}" --journal "${KNOWHERE_OPS_BACKUP_STATE:?}" --approval "${KNOWHERE_OPS_APPROVAL:?}" --operation-id "${KNOWHERE_OPS_BACKUP_OPERATION_ID:?}" --epoch "${KNOWHERE_OPS_EXPECTED_EPOCH:?}" --source-hash "${KNOWHERE_OPS_SOURCE_HASH:?}" --operation-state-directory "${KNOWHERE_OPS_STATE_DIRECTORY:?}" --output "${KNOWHERE_OPS_RECEIPTS_DIRECTORY:?}/backup-$(date +%s).json"',
                "",
            ]),
            "monitor-check.sh": "\n".join([
                "#!/usr/bin/env bash", "set -euo pipefail", 'cd "${KNOWHERE_OPS_REPOSITORY:?}"',
                'exec "${KNOWHERE_OPS_PYTHON:?}" deploy/opensearch/monitor.py --manifest "${KNOWHERE_OPS_MANIFEST:?}" --metrics "${KNOWHERE_OPS_METRICS:?}" --backup-journal "${KNOWHERE_OPS_BACKUP_STATE:?}" --sample-application --state "${KNOWHERE_OPS_MONITOR_STATE:?}" --metrics-max-age-seconds "${KNOWHERE_OPS_METRICS_MAX_AGE_SECONDS:?}" --repeat-seconds "${KNOWHERE_OPS_ALERT_REPEAT_SECONDS:?}" --execute --send-existing-logfire',
                "",
            ]),
            "config.yml": {"_meta": {"type": "config", "config_version": 2}, "config": {"dynamic": {"http": {"anonymous_auth_enabled": False}, "authc": {"clientcert_auth_domain": {
                "http_enabled": True, "transport_enabled": True, "order": 0,
                "http_authenticator": {"type": "clientcert", "challenge": False, "config": {}},
                "authentication_backend": {"type": "noop"},
            }}}}},
            "roles.yml": OpenSearchOperations._renderRoles(installation),
            "full-restore-roles.yml": OpenSearchOperations._renderRoles(installation, isIndependent=True),
            "full-restore-roles_mapping.yml": {"_meta": {"type": "rolesmapping", "config_version": 2},
                "knowhere_backup": {"users": [installation.backup_dn]},
                "knowhere_monitor": {"users": [installation.monitor_dn]}},
            "roles_mapping.yml": {"_meta": {"type": "rolesmapping", "config_version": 2},
                **{f"knowhere_{role}": {"users": [identity]} for role, identity in (
                    ("api", installation.api_dn), ("worker", installation.worker_dn),
                    ("backup", installation.backup_dn), ("monitor", installation.monitor_dn))}},
            "client-environment.json": {
                "KNOWHERE_OPENSEARCH_URL": f"https://{installation.endpoint_host}:9200",
                "KNOWHERE_OPENSEARCH_CA_FILE": "/run/secrets/knowhere-native/ca.pem",
                "KNOWHERE_OPENSEARCH_CLIENT_CERT_FILE": "/run/secrets/knowhere-native/client.pem",
                "KNOWHERE_OPENSEARCH_CLIENT_KEY_FILE": "/run/secrets/knowhere-native/client-key.pem",
            },
            "installation-receipt.json": {
                "schema_version": "opensearch-installation-receipt-v1", "manifest_sha256": _digest(value),
                "candidate_version": "3.9.0", "architecture": "arm64", "heap_gib": 16,
                "source_revision": installation.source_revision,
                "archive_path": installation.archive_path, "archive_sha512": installation.archive_sha512,
                "plugin_path": installation.plugin_path, "plugin_sha512": installation.plugin_sha512,
                "approval_required": True, "provisioned": False,
            },
        }

    @staticmethod
    def _renderRoles(installation: _Installation, *, isIndependent: bool = False) -> dict[str, object]:
        clusterRead: list[str] = ["cluster:monitor/main", "cluster:monitor/state", "cluster:monitor/health"]
        search: str = "indices:data/read/search*"
        indexRead: list[str] = [search, "indices:admin/get", "indices:monitor/settings/get", "indices:monitor/stats"]
        publication: list[str] = ["indices:admin/create", "indices:admin/open", "indices:admin/close", "indices:data/write/bulk*", "indices:data/write/index", "indices:data/read/count"]
        scoped: str = installation.index_prefix + "-*"
        # Namespace indexes use a 48-character fingerprint. Keep their actions off the manifest index.
        namespacePattern: str = f"/{installation.index_prefix}-[a-f0-9]{{48}}/"
        manifestIndex: str = f"{installation.index_prefix}-published-manifests"
        # OpenSearch authorizes _create through the same index action as _doc.
        manifestActions: list[str] = ["indices:admin/create", "indices:data/read/get", "indices:data/write/index"]
        deploymentProbe: str = f"{installation.index_prefix}-deployment-probe"
        applicationCluster: list[str] = [*clusterRead, "indices:data/write/bulk"]
        roles: dict[str, object] = {"_meta": {"type": "roles", "config_version": 2},
            "knowhere_api": {"cluster_permissions": applicationCluster, "index_permissions": [
                {"index_patterns": [namespacePattern], "allowed_actions": [*indexRead, *publication]},
                {"index_patterns": [manifestIndex], "allowed_actions": manifestActions},
                {"index_patterns": [deploymentProbe], "allowed_actions": [search]},
            ]},
            "knowhere_worker": {"cluster_permissions": applicationCluster, "index_permissions": [
                {"index_patterns": [namespacePattern], "allowed_actions": [*indexRead, *publication, "indices:data/write/delete/byquery"]},
                {"index_patterns": [manifestIndex], "allowed_actions": [*manifestActions, "indices:data/write/delete"]},
                {"index_patterns": [deploymentProbe], "allowed_actions": [search]},
            ]},
            "knowhere_backup": {"cluster_permissions": [*clusterRead, "cluster:admin/snapshot/create", "cluster:admin/snapshot/get", "cluster:admin/snapshot/status*", "cluster:admin/snapshot/delete", "cluster:admin/repository/get"], "index_permissions": [{"index_patterns": [scoped], "allowed_actions": ["indices:admin/get", "indices:monitor/settings/get", "indices:monitor/stats"]}]},
            "knowhere_monitor": {"cluster_permissions": [*clusterRead, "cluster:monitor/nodes/stats", "cluster:admin/snapshot/get"], "index_permissions": [{"index_patterns": [scoped], "allowed_actions": ["indices:monitor/stats", "indices:monitor/settings/get"]}]},
        }
        if isIndependent:
            roles.pop("knowhere_api")
            roles.pop("knowhere_worker")
            roles["knowhere_backup"] = {"cluster_permissions": [*clusterRead, "cluster:monitor/nodes/info",
                "cluster:admin/snapshot/get", "cluster:admin/snapshot/status*", "cluster:admin/snapshot/restore",
                "cluster:admin/repository/get"],
                "index_permissions": [
                    {"index_patterns": ["*"], "allowed_actions": ["indices:monitor/stats", "indices:monitor/settings/get"]},
                    {"index_patterns": [scoped, "verify-*-" + scoped], "allowed_actions": [
                        "indices:admin/create", "indices:data/write/index"]},
                    {"index_patterns": ["verify-*-" + scoped], "allowed_actions": [
                        *indexRead, "indices:admin/close", "indices:admin/delete"]},
                ]}
        return roles

    @staticmethod
    def _renderInfrastructure(installation: _Installation, *, isIndependent: bool = False) -> dict[str, object]:
        bucketPrefixes: dict[str, list[str]] = {}
        for bucket, prefix in ((installation.live_bucket, installation.segment_prefix),
                (installation.live_bucket, installation.translog_prefix),
                (installation.live_bucket, installation.state_prefix),
                (installation.backup_bucket, installation.snapshot_prefix)):
            bucketPrefixes.setdefault(bucket, []).append(prefix)
        statements: list[dict[str, object]] = []
        for bucket, prefixes in bucketPrefixes.items():
            statements.extend([
                {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:ListBucketMultipartUploads"], "Resource": [f"arn:aws:s3:::{bucket}"], "Condition": {"StringLike": {"s3:prefix": [pattern for prefix in prefixes for pattern in (prefix, prefix + "/*")]}}},
                {"Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": [f"arn:aws:s3:::{bucket}"]},
                {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"], "Resource": [f"arn:aws:s3:::{bucket}/{prefix}/*" for prefix in prefixes]},
            ])
        if installation.existing_kms_key_arn:
            statements.append({"Effect": "Allow", "Action": ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"], "Resource": [installation.existing_kms_key_arn]})
        if isIndependent:
            live: list[str] = [installation.segment_prefix, installation.translog_prefix, installation.state_prefix]
            statements = [
                {"Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": [f"arn:aws:s3:::{installation.backup_bucket}"]},
                {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": [f"arn:aws:s3:::{installation.backup_bucket}"],
                    "Condition": {"StringLike": {"s3:prefix": [installation.snapshot_prefix, installation.snapshot_prefix + "/*"]}}},
                {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{installation.backup_bucket}/{installation.snapshot_prefix}/*"]},
                {"Effect": "Deny", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                    "Resource": [f"arn:aws:s3:::{installation.live_bucket}/{prefix}/*" for prefix in live]},
                {"Effect": "Deny", "Action": ["s3:ListBucket"], "Resource": [f"arn:aws:s3:::{installation.live_bucket}"],
                    "Condition": {"StringLike": {"s3:prefix": [prefix + "*" for prefix in live]}}},
            ]
            if installation.existing_kms_key_arn:
                statements.append({"Effect": "Allow", "Action": ["kms:Decrypt", "kms:DescribeKey"], "Resource": [installation.existing_kms_key_arn]})
        resources: dict[str, object] = {
            "NativeSecurityGroup": {"Type": "AWS::EC2::SecurityGroup", "Properties": {
                "GroupDescription": "Private Knowhere native clients; no public ingress",
                "VpcId": installation.vpc_id,
                "SecurityGroupIngress": [{"IpProtocol": "tcp", "FromPort": 9200, "ToPort": 9200, "SourceSecurityGroupId": identity}
                    for identity in ((installation.management_security_group,) if isIndependent else
                        (installation.api_security_group, installation.worker_security_group, installation.management_security_group))],
                "SecurityGroupEgress": [
                    {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "DestinationPrefixListId": installation.s3_prefix_list},
                    {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "DestinationSecurityGroupId": installation.management_security_group},
                ],
            }},
            "NativeRole": {"Type": "AWS::IAM::Role", "Properties": {
                "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]},
                "Policies": [{"PolicyName": "ScopedNativeRepositories", "PolicyDocument": {"Version": "2012-10-17", "Statement": statements}}],
                "ManagedPolicyArns": ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"],
            }},
            "NativeProfile": {"Type": "AWS::IAM::InstanceProfile", "Properties": {"Roles": [{"Ref": "NativeRole"}]}},
            "NativeNode": {"Type": "AWS::EC2::Instance", "Properties": {
                "InstanceType": "r7g.xlarge", "ImageId": installation.ami_id,
                "AvailabilityZone": installation.availability_zone,
                "IamInstanceProfile": {"Ref": "NativeProfile"},
                "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1},
                "NetworkInterfaces": [{"DeviceIndex": "0", "SubnetId": installation.subnet_id, "AssociatePublicIpAddress": False, "GroupSet": [{"Ref": "NativeSecurityGroup"}]}],
                "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeType": "gp3", "VolumeSize": 20, "Encrypted": True, "DeleteOnTermination": True}}],
                "Tags": [{"Key": "Name", "Value": installation.cluster_name}, {"Key": "Environment", "Value": installation.environment}],
            }},
            "NativeDataVolume": {"Type": "AWS::EC2::Volume", "DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain", "Properties": {
                "AvailabilityZone": installation.availability_zone, "VolumeType": "gp3", "Size": 100,
                "Iops": 3000, "Throughput": 125, "Encrypted": True,
                **({"KmsKeyId": installation.existing_kms_key_arn} if installation.existing_kms_key_arn else {}),
                "Tags": [{"Key": "Name", "Value": installation.cluster_name + "-data"}],
            }},
            "NativeDataAttachment": {"Type": "AWS::EC2::VolumeAttachment", "Properties": {"InstanceId": {"Ref": "NativeNode"}, "VolumeId": {"Ref": "NativeDataVolume"}, "Device": "/dev/sdf"}},
        }
        return {"AWSTemplateFormatVersion": "2010-09-09", "Description": "Review-only selected private native baseline; cost approval required before execution", "Resources": resources,
            "Outputs": {"InstanceId": {"Value": {"Ref": "NativeNode"}}, "RetainedDataVolumeId": {"Value": {"Ref": "NativeDataVolume"}}, "NativeSecurityGroupId": {"Value": {"Ref": "NativeSecurityGroup"}}}}

    @staticmethod
    def planSnapshot(inventory: object, *, boundarySha256: str, snapshotName: str) -> dict[str, object]:
        indexes: list[_Index] = _validateInventory(inventory)
        if len(boundarySha256) != 64 or not snapshotName or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for character in snapshotName):
            raise ValueError("Snapshot requires a frozen boundary digest and explicit safe name")
        return {"schema_version": "opensearch-snapshot-plan-v1", "coverage_verified": False,
            "inventory": [index.model_dump() for index in indexes], "inventory_sha256": _digest(inventory),
            "boundary_sha256": boundarySha256,
            "request": {"method": "PUT", "path": f"/_snapshot/knowhere-full/{snapshotName}?wait_for_completion=false", "body": {
                "indices": [index.name for index in indexes], "ignore_unavailable": False, "expand_wildcards": "all",
                "include_global_state": False, "partial": False,
                "metadata": {"inventory_sha256": _digest(inventory), "boundary_sha256": boundarySha256},
            }},
            "verify_paths": [f"/_snapshot/knowhere-full/{snapshotName}", f"/_snapshot/knowhere-full/{snapshotName}/_status"],
            "required_followups": ["poll-completion", "verify-index-shard-catalog-and-boundary", "persist-verified-point", "native-retention"],
        }

    @staticmethod
    def verifySnapshot(inventory: object, snapshot: dict[str, object], *, observedInventory: object,
            boundarySha256: str, verifiedBoundarySha256: str | None, shardStatus: object, verifiedAt: int,
            boundaryCapturedAtMillis: int | None = None) -> dict[str, object]:
        indexes: list[_Index] = _validateInventory(inventory)
        observed: list[_Index] = _validateInventory(observedInventory)
        errors: list[str] = []
        if snapshot.get("state") != "SUCCESS" or snapshot.get("failures"):
            errors.append("Snapshot did not complete successfully")
        metadata: object = snapshot.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("inventory_sha256") != _digest(inventory) or metadata.get("boundary_sha256") != boundarySha256:
            errors.append("Snapshot metadata does not bind the approved inventory and boundary hashes")
        if snapshot.get("remote_store_index_shallow_copy") is not False or snapshot.get("shallow_snapshot_v2", False) is not False:
            errors.append("Snapshot is not an independent full copy")
        if set(snapshot.get("indices", [])) != {index.name for index in indexes}:  # type: ignore[arg-type]
            errors.append("Snapshot does not exactly cover expected open and closed catalog")
        capturedNames: set[str] = {index.name for index in indexes}
        if {index.name: index.uuid for index in indexes} != {index.name: index.uuid for index in observed if index.name in capturedNames}:
            errors.append("Catalog identities changed during backup; retry and reconcile coverage")
        expectedShards: int = sum(index.primary_shards for index in indexes)
        if snapshot.get("shards") != {"total": expectedShards, "successful": expectedShards, "failed": 0}:
            errors.append("Snapshot does not cover every expected primary shard")
        try:
            status: _SnapshotStatus = _Statuses.model_validate(shardStatus).snapshots[0]
            if status.uuid != snapshot.get("uuid") or status.snapshot != snapshot.get("snapshot") or status.state != "SUCCESS":
                errors.append("Shard status does not identify the same successful snapshot")
            if set(status.indices) != {index.name for index in indexes}:
                errors.append("Shard status does not cover the complete catalog")
            for index in indexes:
                observedShards: dict[str, _Shard] = status.indices[index.name].shards if index.name in status.indices else {}
                if set(observedShards) != {str(number) for number in range(index.primary_shards)} or any(shard.stage != "DONE" for shard in observedShards.values()):
                    errors.append("An expected index has missing or unfinished snapshot shards")
        except ValidationError:
            errors.append("Complete per-index shard status was not supplied")
        if verifiedBoundarySha256 != boundarySha256:
            errors.append("Document/revision/build/update/delete boundary is not independently verified")
        started: int = int(str(snapshot.get("start_time_in_millis", 0)))
        completed: int = int(str(snapshot.get("end_time_in_millis", 0)))
        if not started or completed < started or verifiedAt < completed:
            errors.append("Snapshot timing/completion verification is inconsistent")
        if boundaryCapturedAtMillis is None or not 0 < boundaryCapturedAtMillis <= started:
            errors.append("Verified data-boundary capture time is missing or later than snapshot start")
        return {"schema_version": "opensearch-verified-point-v1", "snapshot": snapshot.get("snapshot"),
            "snapshot_uuid": snapshot.get("uuid"), "state": snapshot.get("state"),
            "coverage_verified": not errors, "errors": errors,
            "inventory": [index.model_dump() for index in indexes], "inventory_sha256": _digest(inventory),
            "current_catalog_additions": sorted(index.name for index in observed if index.name not in capturedNames),
            "boundary_sha256": boundarySha256, "verified_boundary_sha256": verifiedBoundarySha256,
            "started_at_millis": started, "completed_at_millis": completed,
            "duration_millis": completed - started, "verified_at_millis": verifiedAt,
            "completion_age_millis": verifiedAt - completed,
            "data_boundary_captured_at_millis": boundaryCapturedAtMillis,
            "data_boundary_age_millis": verifiedAt - boundaryCapturedAtMillis if boundaryCapturedAtMillis is not None else None,
        }

    @staticmethod
    def planRetention(points: list[object], *, nowMillis: int) -> dict[str, object]:
        parsed: list[_Point] = [_Point.model_validate(point) for point in points]
        if len({point.snapshot for point in parsed}) != len(parsed):
            raise ValueError("Snapshot inventory contains duplicate identities")
        verified: list[_Point] = sorted((point for point in parsed if point.state == "SUCCESS" and point.coverage_verified), key=lambda point: point.completed_at_millis, reverse=True)
        protected: list[str] = [point.snapshot for point in verified[:3]]
        delete: list[str] = [point.snapshot for point in parsed if point.state != "IN_PROGRESS" and point.snapshot not in protected and point.completed_at_millis < nowMillis - 7 * _DAY_MILLISECONDS]
        return {"protected": protected, "delete": delete, "verified_recovery_point_count": len(verified),
            "retention_floor_satisfied": len(verified) >= 3,
            "deletion_api": "DELETE /_snapshot/knowhere-full/{snapshot}", "s3_object_ttl": False}

    @staticmethod
    def planRestore(indices: list[str], *, openCount: int, reservedCount: int, uncertainCount: int, batchLimit: int) -> dict[str, object]:
        if min(openCount, reservedCount, uncertainCount) < 0 or batchLimit < 1 or len(set(indices)) != len(indices):
            raise ValueError("Restore requires exact nonnegative capacity and unique index identities")
        available: int = 400 - openCount - reservedCount - uncertainCount
        if available <= 0:
            raise ValueError("No business restore capacity is available before reservation")
        size: int = min(available, batchLimit)
        return {"schema_version": "opensearch-restore-batches-v1", "recheck_capacity_before_each_batch": True,
            "requires_application_gate_and_single_writer_fence": True,
            "batches": [{"indices": indices[offset:offset + size], "reserved_slots": len(indices[offset:offset + size]),
                "actions": ["reserve", "restore", "verify-identities-and-boundary", "close-idle", "reconcile-and-release"]}
                for offset in range(0, len(indices), size)],
            "restore_body_defaults": {"ignore_unavailable": False, "include_global_state": False, "include_aliases": False},
        }

    @staticmethod
    def planRecovery(route: Literal["process", "healthy-volume", "live-remote", "full-snapshot"], fence: object,
            *, requiredBoundary: str, restoredBoundary: str | None) -> dict[str, object]:
        proof: _Fence = _Fence.model_validate(fence)
        if not (proof.old_issuer_stopped and proof.application_gated and proof.native_writers_settled and proof.new_epoch > proof.old_epoch):
            raise ValueError("Old issuer and application publication must be fenced before recovery")
        covered: bool = bool(requiredBoundary) and requiredBoundary == restoredBoundary
        steps: dict[str, list[str]] = {
            "process": ["verify-old-process-stopped", "systemctl-restart", "reconcile-native-identities"],
            "healthy-volume": ["verify-original-instance-stopped", "same-AZ-replacement", "attach-retained-volume", "mount-existing-filesystem", "start-pinned-runtime", "reconcile-native-identities"],
            "live-remote": ["verify-old-instance-fenced", "approved-replacement", "restore-pinned-remote-state-config", "restore-native-remote-shards-in-admitted-batches", "reconcile-native-identities"],
            "full-snapshot": ["verify-old-instance-fenced", "deny-live-repository-access-for-drill", "register-full-repository-readonly", "admission-aware-full-restore", "reconcile-native-identities"],
        }
        return {"schema_version": "opensearch-recovery-plan-v1", "route": route, "fence": proof.model_dump(),
            "steps": [*steps[route], "verify-required-current-published-boundary", "verify-canonical-api-hydration"],
            "historical_snapshot_restored": False,
            "historical_boundary_supplied": route == "full-snapshot" and restoredBoundary is not None,
            "published_boundary_matches": covered,
            "full_service_restored": False, "reopen_traffic": False,
            "requires_actual_hydration_receipt_before_reopen": True,
            "rebuild_from_postgresql": False, "rewind_published_pointers": False,
            "unavailable_and_escalate": not covered,
            "disruption_to_serving_objective_seconds": 900 if route in ("process", "healthy-volume") else 3600,
            "includes_detection_human_response_failed_attempts": True, "rto_passed": False,
        }

    @staticmethod
    def evaluateAlerts(metrics: object, previous: dict[str, object], *, nowSeconds: int, repeatSeconds: int) -> dict[str, object]:
        observed: _Metrics = _Metrics.model_validate(metrics)
        if repeatSeconds < 1:
            raise ValueError("Alert repeat interval must be positive")
        conditions: dict[str, bool] = {
            "native-unavailable": not observed.native_available,
            "retrieval-unavailable": observed.retrieval_unavailable,
            "final-indexing-failure": observed.final_indexing_failure,
            "identity-mismatch": observed.identity_failure,
            "indexing-age": observed.indexing_oldest_seconds > 300,
            "pg-copy-age": observed.pg_copy_oldest_seconds > 1800,
            "pg-copy-final-failure": observed.pg_copy_final_failure,
            "backup-age": observed.latest_complete_snapshot_age_seconds is None or observed.latest_complete_snapshot_age_seconds > 7200,
            "backup-incomplete": observed.backup_incomplete,
            **{f"resource-{name}": active for name, active in observed.resource_pressure.items()},
        }
        events: list[dict[str, object]] = []
        state: dict[str, object] = {}
        for condition, active in conditions.items():
            old: _AlertState = _AlertState.model_validate(previous.get(condition, {"active": False, "last_emitted_seconds": 0}))
            transition: str | None = None
            if active and not old.active:
                transition = "firing"
            elif active and nowSeconds - old.last_emitted_seconds >= repeatSeconds:
                transition = "repeat"
            elif not active and old.active:
                transition = "resolved"
            if transition:
                events.append({"condition": condition, "transition": transition, "severity": "info" if transition == "resolved" else "error", "observed_at_seconds": nowSeconds})
            state[condition] = {"active": active, "last_emitted_seconds": nowSeconds if transition else old.last_emitted_seconds}
        return {"schema_version": "opensearch-offnode-alerts-v1", "events": events, "state": state,
            "mutates_job_or_backend": False, "notification_delivery_verified": False}

    @staticmethod
    def planRelease(firstImpact: datetime, *, restorationSeconds: int, marginSeconds: int) -> dict[str, object]:
        if firstImpact.tzinfo is None or restorationSeconds <= 0 or marginSeconds <= 0 or restorationSeconds + marginSeconds >= 7200:
            raise ValueError("A release requires measured restoration, explicit positive margin, and a viable two-hour budget")
        return {"schema_version": "opensearch-two-release-plan-v1", "first_customer_impact_at": firstImpact.isoformat(),
            "measured_restoration_seconds": restorationSeconds, "margin_seconds": marginSeconds,
            "latest_abort_at": (firstImpact + timedelta(seconds=7200 - restorationSeconds - marginSeconds)).isoformat(),
            "release_one": ["deploy-compatible-PG-primary-contracts", "drain-old-finalizers", "install-approved-native-baseline", "backfill-current-published", "verify-correctness-quality-load-recovery-PG-coverage"],
            "forward_verbs": ["inspect", "gate", "settle", "verify", "resume"],
            "rollback_verbs": ["gate", "settle", "backfill", "verify", "abort", "resume"],
            "transition_tool": "scripts/probes/manage-opensearch-transition.py",
            "required_actual_receipts": ["source-and-config", "compatible-API-worker-SDKs", "native-current-published-coverage", "PG-rollback-coverage", "quality", "mixed-load", "native-recovery", "full-snapshot-coverage", "alert-delivery", "manual-response-coverage", "restoration-rehearsal", "production-and-cost-approval"],
            "automatic_retry_after_resume": False, "schema_downgrade": False,
            "preserve_parsing_result_charge_and_logical_events": True,
            "wait_for_all_parsing_jobs": False, "executed": False,
        }

    @staticmethod
    def planCost(quote: object, usage: object) -> dict[str, object]:
        rates: _Quote = _Quote.model_validate(quote)
        profile: _Usage = _Usage.model_validate(usage)
        amounts: dict[str, float] = {
            "compute_730_hours": 730 * rates.compute_hour,
            "gp3_120_gib": 120 * rates.gp3_gib_month,
            "remote_and_independent_snapshot_storage": (profile.remote_gib + profile.snapshot_gib) * rates.s3_gib_month,
            "put_requests": profile.put_requests / 1000 * rates.put_per_thousand,
            "get_requests": profile.get_requests / 1000 * rates.get_per_thousand,
            "network": profile.network_gib * rates.network_gib,
            "existing_kms_usage_if_applicable": profile.kms_requests / 10_000 * rates.kms_per_ten_thousand,
        }
        recoveryAmounts: dict[str, float] = {
            "compute": profile.temporary_recovery_hours * rates.compute_hour,
            "additional_gp3": profile.temporary_recovery_extra_gib * rates.gp3_gib_month * profile.temporary_recovery_hours / 730,
            "additional_S3": profile.temporary_recovery_s3_gib_month_equivalent * rates.s3_gib_month,
            "put_list_requests": profile.temporary_recovery_put_requests / 1000 * rates.put_per_thousand,
            "get_requests": profile.temporary_recovery_get_requests / 1000 * rates.get_per_thousand,
            "network": profile.temporary_recovery_network_gib * rates.network_gib,
            "existing_kms_requests": profile.temporary_recovery_kms_requests / 10_000 * rates.kms_per_ten_thousand,
        }
        return {"schema_version": "opensearch-bounded-cost-plan-v1", "quote": rates.model_dump(), "usage": profile.model_dump(),
            "monthly_components_usd": amounts, "monthly_estimated_usd": round(sum(amounts.values()), 6),
            "temporary_recovery_components_usd": recoveryAmounts,
            "temporary_recovery_estimated_usd": round(sum(recoveryAmounts.values()), 6),
            "temporary_recovery_hours": profile.temporary_recovery_hours,
            "excluded_unapproved_features": ["new-NAT", "new-KMS-key", "read-replica", "second-always-on-node"],
            "resource_quantities_and_request_churn_require_actual_campaign_review": True,
            "quote_is_actual_bill": False, "approved_to_provision": False}

    @staticmethod
    def preflight(value: object, *, proofs: dict[str, object]) -> dict[str, object]:
        installation: _Installation = _Installation.model_validate(value)
        identity: str = _digest(value)
        required: list[str] = [
            "explicit-cost-approval", "bounded-recurring-and-recovery-cost-plan",
            "private-S3-gateway-route-and-policies", "ARM64-AMI-and-pinned-plugin-artifacts",
            "private-DNS-TLS-service-and-admin-certificates", "existing-VPC-subnet-and-AZ-placement",
            "S3-IAM-encryption-key-recovery-and-lifecycle", "canonical-results-and-input-no-age-only-expiry",
            "offnode-scheduler-alert-path-and-manual-response", "sync-and-async-authenticated-client-acceptance",
        ]
        missing: list[str] = []
        for name in required:
            proof: object = proofs.get(name)
            if not isinstance(proof, dict) or proof.get("manifest_sha256") != identity or proof.get("status") != "passed" or proof.get("mode") != "actual" or not proof.get("artifact_sha256"):
                missing.append(name)
        return {"schema_version": "opensearch-infrastructure-preflight-v1", "manifest_sha256": identity,
            "environment": installation.environment, "ready_to_provision": not missing, "missing": missing,
            "provisioned": False, "requires_explicit_user_cost_approval": True,
            "read_only_preflight_commands": [
                ["aws", "ec2", "describe-images", "--image-ids", installation.ami_id, "--region", installation.region],
                ["aws", "ec2", "describe-subnets", "--subnet-ids", installation.subnet_id, "--region", installation.region],
                ["aws", "ec2", "describe-route-tables", "--filters", f"Name=vpc-id,Values={installation.vpc_id}", "--region", installation.region],
                *[["aws", "s3api", operation, "--bucket", bucket] for bucket in sorted({installation.live_bucket, installation.backup_bucket}) for operation in ("get-bucket-encryption", "get-bucket-policy", "get-bucket-lifecycle-configuration")],
            ]}
