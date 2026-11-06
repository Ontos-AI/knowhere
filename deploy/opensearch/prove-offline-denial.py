"""Check live-repository denial using the approved offline node's real instance role."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Protocol, cast

import httpx

from execution import OperationExecution
from manage import _readJson, _requireObject, _validateApproval, _writeJson
from operations import OpenSearchOperations


class _S3(Protocol):
    def head_object(self, **values: object) -> dict[str, object]: ...
    def list_objects_v2(self, **values: object) -> dict[str, object]: ...


def _verifyDenial(s3: _S3, *, bucket: str, prefixes: list[str], sentinelKeys: list[str],
        isDenied: Callable[[Exception], bool]) -> list[dict[str, str]]:
    if len(prefixes) != 3 or len(sentinelKeys) != 3 or any(not key.startswith(prefix.rstrip("/") + "/")
            for key, prefix in zip(sentinelKeys, prefixes, strict=True)):
        raise ValueError("Bind one previously observed existing live object per exact repository prefix")
    checks: list[dict[str, str]] = []
    for prefix, key in zip(prefixes, sentinelKeys, strict=True):
        for method, invoke in (("HeadObject", lambda: s3.head_object(Bucket=bucket, Key=key)),
                ("ListObjectsV2", lambda: s3.list_objects_v2(Bucket=bucket, Prefix=prefix.rstrip("/") + "/", MaxKeys=1))):
            try:
                invoke()
            except Exception as error:
                if not isDenied(error):
                    raise ValueError("Cannot prove actual denied live repository access") from error
            else:
                raise ValueError("Dedicated offline instance role can access the live repository")
            checks.append({"operation": method, "prefix": prefix, "status": "denied"})
    return checks


def _main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args: argparse.Namespace = parser.parse_args()
    manifest: dict[str, object] = _requireObject(_readJson(args.manifest))
    plan: dict[str, object] = _requireObject(_readJson(args.plan))
    installation: dict[str, object] = _requireObject(OpenSearchOperations.renderBundle(manifest)["installation-receipt.json"])
    planDigest: str = sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if not args.execute:
        print(json.dumps({"mode": "dry-run", "status": "planned", "plan_sha256": planDigest,
            "network_calls": 0, "mutations": 0, "expected_live_repository_reads": 6}))
        return 0
    _validateApproval(args.approval, manifestSha256=str(installation["manifest_sha256"]), purpose="backup")
    approval: dict[str, object] = _requireObject(_readJson(args.approval))
    if (args.output is None or args.output.exists() or approval.get("operation_plan_sha256") != planDigest
            or plan.get("offline_host_sha256") != OperationExecution.getHostFingerprint()):
        raise ValueError("Offline denial verification requires the exact approved host, operation and new receipt")
    root: Path = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "packages/shared-python"))
    from boto3.session import Session
    from botocore.config import Config
    from botocore.exceptions import ClientError
    from shared.services.retrieval.lexical.clientconfig import NativeClientConfig
    from shared.services.retrieval.lexical.transition import LexicalTransition

    if approval.get("source_hash") != LexicalTransition.getSourceHash():
        raise ValueError("Offline denial verifier differs from reviewed source")
    endpoint: str = str(plan["target_endpoint"])
    native: NativeClientConfig = NativeClientConfig.resolve(endpoint, environmentPrefix="KNOWHERE_OFFLINE_OPENSEARCH_")
    if not native.endpoint.startswith("https://") or not str(plan["target_cluster_uuid"]) or plan["target_cluster_uuid"] == plan["source_cluster_uuid"]:
        raise ValueError("Proof target must be an independent private native cluster")
    with httpx.Client(base_url=native.endpoint, auth=native.auth, verify=native.verify, timeout=5, trust_env=False) as client:
        response: httpx.Response = client.get("/_cluster/state/nodes?filter_path=cluster_uuid,nodes.*.ephemeral_id")
        response.raise_for_status()
        cluster: dict[str, object] = _requireObject(response.json())
        nodes: dict[str, object] = _requireObject(cluster.get("nodes"))
        if (cluster.get("cluster_uuid") != plan["target_cluster_uuid"] or len(nodes) != 1
                or _requireObject(next(iter(nodes.values()))).get("ephemeral_id") != plan["target_incarnation"]):
            raise ValueError("Denial proof target identity differs from reviewed independent node")
    session = Session(region_name=str(manifest["region"]))
    credentials = session.get_credentials()
    if credentials is None or credentials.method != "iam-role":
        raise ValueError("Use the offline native EC2 instance profile, not environment/user credentials")
    config = Config(connect_timeout=5, read_timeout=5, retries={"total_max_attempts": 1, "mode": "standard"})
    caller: dict[str, str] = cast(dict[str, str], session.client("sts", config=config).get_caller_identity())
    if caller["Account"] != plan["account_id"] or caller["Arn"] != plan["offline_instance_role_session_arn"]:
        raise ValueError("AWS caller does not match the exact offline native instance role session")

    def denied(error: Exception) -> bool:
        return (isinstance(error, ClientError) and error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 403
            and error.response.get("Error", {}).get("Code") in ("AccessDenied", "Forbidden", "403"))

    prefixes: list[str] = [str(manifest[key]) for key in ("segment_prefix", "translog_prefix", "state_prefix")]
    checks: list[dict[str, str]] = _verifyDenial(cast(_S3, session.client("s3", config=config)),
        bucket=str(manifest["live_bucket"]), prefixes=prefixes, sentinelKeys=cast(list[str], plan["existing_sentinel_keys"]), isDenied=denied)
    _writeJson(args.output, {"mode": "actual", "status": "denied", "source_hash": approval["source_hash"],
        "plan_sha256": planDigest, "bucket": manifest["live_bucket"], "prefixes": prefixes,
        "source_cluster_uuid": plan["source_cluster_uuid"], "target_cluster_uuid": plan["target_cluster_uuid"],
        "target_incarnation": plan["target_incarnation"], "checks": checks, "account_id": caller["Account"],
        "verified_at_utc": datetime.now(timezone.utc).isoformat()})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except Exception as error:
        print("Independent repository denial proof was refused: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(2) from None
