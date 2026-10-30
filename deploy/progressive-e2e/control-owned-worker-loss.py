"""Kill one provably owned Fine task and record its unchanged one-hour claim.

Run on the Docker host. This test tool never changes claim timestamps, worker
configuration, queue configuration, or product code. Runtime mutation requires
the explicit --kill-and-restart flag. Broker reads work with remote control off.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

TASK_NAME: str = "app.core.tasks.enrichment_tasks.enrich_page_memory_scope"
COMPOSE_PROJECT: str = "knowhere-progressive-e2e"
CLAIM_SECONDS: int = 3600
BROKER_PROBE: str = """
import base64,json
from shared.core.celery_app import celery_app
from shared.core.config import app_config
rows=[]
with celery_app.connection_for_read() as connection:
    channel=connection.channel()
    for raw in channel.client.hgetall(channel.qos.unacked_key).values():
        message=json.loads(raw)[0]
        headers=message.get('headers',{})
        name=headers.get('task','')
        arguments=[]
        if name=='app.core.tasks.enrichment_tasks.enrich_page_memory_scope':
            body=message.get('body','')
            if message.get('properties',{}).get('body_encoding')=='base64':
                body=base64.b64decode(body)
            arguments=json.loads(body)[0]
        rows.append({'taskId':headers.get('id'),'taskName':name,'arguments':arguments})
print('OWNED_LOSS_PROBE='+json.dumps({'claimSeconds':app_config.TASK_TIME_LIMIT_SECONDS,'remoteControlEnabled':celery_app.conf.worker_enable_remote_control,'reservations':rows}))
"""


def runDocker(arguments: list[str]) -> str:
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("A Docker command failed; command output was not disclosed.")
    return result.stdout.strip()


def findContainer(service: str) -> str:
    identifier: str = runDocker([
        "ps", "-q", "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
        "--filter", f"label=com.docker.compose.service={service}",
    ])
    if re.fullmatch(r"[0-9a-f]{12,64}", identifier) is None:
        raise ValueError(f"Expected exactly one running isolated {service} container.")
    return identifier


def readJsonOutput(raw: str, marker: str) -> dict[str, object]:
    lines: list[str] = [line for line in raw.splitlines() if line.startswith(marker)]
    if len(lines) != 1:
        raise ValueError("The reduced probe did not return one JSON result.")
    value: object = json.loads(lines[0][len(marker):])
    if not isinstance(value, dict):
        raise ValueError("The reduced probe returned an invalid result.")
    return value


def readSnapshot(postgres: str, documentId: str, namespace: str) -> dict[str, object]:
    # Both substitutions have already passed strict identifier validation.
    query: str = f"""
BEGIN READ ONLY;
SELECT json_build_object(
  'capturedAt', clock_timestamp(), 'documentId', d.document_id,
  'namespace', d.namespace, 'documentStatus', d.status,
  'jobResultId', r.id, 'jobId', j.job_id, 'jobStatus', j.status,
  'enhancement', r.document_metadata->'progressive'->'enhancement',
  'stages', r.document_metadata->'progressive'->'stages',
  'scopes', coalesce((SELECT json_agg(s) FROM (
    SELECT scope_id, state, attempts, claim_generation, updated_at, last_hit_at
    FROM document_scope_enrichments WHERE job_result_id=r.id ORDER BY scope_id
  ) s), '[]'::json),
  'foreignRunningScopes', (SELECT count(*) FROM document_scope_enrichments
    WHERE state='running' AND job_result_id<>r.id),
  'foreignRunningJobs', (SELECT count(*) FROM jobs
    WHERE status NOT IN ('done','failed','cancelled','canceled') AND job_id<>j.job_id),
  'foreignRunningStages', (SELECT count(*) FROM job_results foreign_revision
    CROSS JOIN LATERAL json_each(coalesce(
      foreign_revision.document_metadata->'progressive'->'stages', '{{}}'::json
    )) stage WHERE foreign_revision.id<>r.id AND stage.value->>'status'='running')
) FROM documents d JOIN job_results r ON r.id=d.current_job_result_id
JOIN jobs j ON j.job_id=r.job_id
WHERE d.document_id='{documentId}' AND d.namespace='{namespace}' AND d.status='active';
ROLLBACK;
"""
    raw: str = runDocker(["exec", postgres, "psql", "-U", "root", "-d", "Knowhere", "-Atq", "-c", query])
    value: object = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("The owned active document could not be resolved.")
    return value


def readScopes(snapshot: dict[str, object]) -> list[dict[str, object]]:
    value: object = snapshot.get("scopes")
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("The scope probe returned an invalid result.")
    return cast(list[dict[str, object]], value)


def readWorkerResources(identifier: str) -> dict[str, object]:
    raw: str = runDocker(["inspect", "--format", "{{json .HostConfig}}", identifier])
    config: dict[str, object] = json.loads(raw)
    if config.get("NanoCpus") != 2_000_000_000 or config.get("Memory") != 4_294_967_296:
        raise ValueError("Expected the existing 2 CPU / 4 GiB Worker configuration.")
    concurrency: str = runDocker(["exec", identifier, "python", "-c", "import os; print(os.environ.get('WORKER_CONCURRENCY',''))"])
    if concurrency != "10":
        raise ValueError("Expected the existing Worker concurrency of 10.")
    return {"nanoCpus": config["NanoCpus"], "memoryBytes": config["Memory"], "concurrency": 10}


def findOwner(workers: dict[str, str], taskId: str, since: str) -> str | None:
    owners: list[str] = []
    for service, identifier in workers.items():
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["docker", "logs", "--since", since, identifier], capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("Worker log collection failed; output was not disclosed.")
        raw: str = result.stdout + result.stderr
        marker: str = f"{TASK_NAME}[{taskId}]"
        if f"{marker} received" in raw and not any(
            f"{marker} {terminal}" in raw
            for terminal in ("succeeded", "raised", "failed", "ignored", "revoked")
        ):
            owners.append(service)
    return owners[0] if len(owners) == 1 else None


def saveReport(output: Path, report: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--kill-and-restart", action="store_true")
    arguments: argparse.Namespace = parser.parse_args()
    manifest: dict[str, object] = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    if isinstance(manifest.get("browser_manifest"), dict):
        manifest = cast(dict[str, object], manifest["browser_manifest"])
    documentId: str = str(manifest.get("documentId", ""))
    namespace: str = str(manifest.get("namespace", ""))
    projectId: str = str(manifest.get("projectId", ""))
    if re.fullmatch(r"doc_[0-9a-f]{12}", documentId) is None:
        raise ValueError("Expected an exact task-owned document identifier.")
    if re.fullmatch(r"hipo-[0-9a-f-]{36}", namespace) is None or re.fullmatch(r"[0-9a-f-]{36}", projectId) is None:
        raise ValueError("The ownership manifest must identify its isolated Hipo namespace.")
    if "isolated-account-and-project" not in manifest.get("stages", []):
        raise ValueError("The ownership manifest does not record an isolated acceptance project.")
    startedAt: str = str(manifest["startedAt"])
    datetime.fromisoformat(startedAt.replace("Z", "+00:00"))
    workers: dict[str, str] = {service: findContainer(service) for service in ("worker-a", "worker-b")}
    resources: dict[str, dict[str, object]] = {service: readWorkerResources(identifier) for service, identifier in workers.items()}
    postgres: str = findContainer("postgres")
    api: str = findContainer("api")
    armedAt: str = datetime.now(timezone.utc).isoformat()
    print(f"WORKER_LOSS_ARMED={armedAt}", flush=True)
    saveReport(arguments.output, {
        "kind": "real-owned-worker-loss", "documentId": documentId,
        "namespace": namespace, "armedAt": armedAt, "status": "waiting-for-running-fine",
        "workerResources": resources,
    })
    deadline: float = time.monotonic() + arguments.timeout_seconds
    lastObservation: str | None = None
    while time.monotonic() < deadline:
        snapshot: dict[str, object] = readSnapshot(postgres, documentId, namespace)
        if any(snapshot[name] != 0 for name in ("foreignRunningScopes", "foreignRunningJobs", "foreignRunningStages")):
            raise ValueError("Other work is running; no Worker may be killed by this controller.")
        running: list[dict[str, object]] = [row for row in readScopes(snapshot) if row["state"] == "running"]
        if not running:
            observation: str = json.dumps({"scopeStates": [{"scopeId": row["scope_id"], "state": row["state"], "attempts": row["attempts"]} for row in readScopes(snapshot)]})
            if observation != lastObservation:
                print(observation, flush=True)
                lastObservation = observation
            if all(row["state"] in ("refined", "failed") for row in readScopes(snapshot)):
                raise ValueError("Fine settled before a lost claim was captured; use a fresh untouched document.")
            time.sleep(0.25)
            continue
        broker: dict[str, object] = readJsonOutput(runDocker(["exec", api, "python", "-c", BROKER_PROBE]), "OWNED_LOSS_PROBE=")
        if broker.get("claimSeconds") != CLAIM_SECONDS or broker.get("remoteControlEnabled") is not False:
            raise ValueError("The original one-hour claim and disabled remote control are required.")
        reservations: object = broker.get("reservations")
        if not isinstance(reservations, list):
            raise ValueError("The broker did not return a reservation list.")
        observation = json.dumps({"runningScopes": [row["scope_id"] for row in running], "brokerReservationCount": len(reservations)})
        if observation != lastObservation:
            print(observation, flush=True)
            lastObservation = observation
        for scope in running:
            matches: list[dict[str, object]] = [row for row in reservations if isinstance(row, dict) and row.get("taskName") == TASK_NAME and row.get("arguments") == [snapshot["jobResultId"], scope["scope_id"]]]
            if len(matches) != 1:
                continue
            taskId: str = str(matches[0].get("taskId"))
            if re.fullmatch(r"[0-9a-f-]{36}", taskId) is None:
                raise ValueError("The broker returned an invalid task identifier.")
            owner: str | None = findOwner(workers, taskId, startedAt)
            if owner is None:
                continue
            before: dict[str, object] = readSnapshot(postgres, documentId, namespace)
            sameClaim: list[dict[str, object]] = [row for row in readScopes(before) if row == scope]
            if not sameClaim:
                continue
            claimAt: datetime = datetime.fromisoformat(str(scope["updated_at"]).replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
            report: dict[str, object] = {
                "kind": "real-owned-worker-loss", "documentId": documentId, "namespace": namespace,
                "jobResultId": snapshot["jobResultId"], "taskId": taskId, "scopeId": scope["scope_id"],
                "workerService": owner, "workerContainerId": workers[owner], "workerResources": resources,
                "remoteControlEnabled": False, "claimSeconds": CLAIM_SECONDS,
                "armedAt": armedAt,
                "reclaimNotBefore": (claimAt + timedelta(seconds=CLAIM_SECONDS)).isoformat(),
                "before": before, "killRequested": arguments.kill_and_restart,
            }
            saveReport(arguments.output, report)
            if arguments.kill_and_restart:
                runDocker(["kill", "--signal", "KILL", workers[owner]])
                report["killedAt"] = datetime.now(timezone.utc).isoformat()
                try:
                    after: dict[str, object] = readSnapshot(postgres, documentId, namespace)
                    report["afterKill"] = after
                    report["hasValidLostClaim"] = scope in readScopes(after)
                finally:
                    runDocker(["start", workers[owner]])
                    report["restartRequestedAt"] = datetime.now(timezone.utc).isoformat()
                    saveReport(arguments.output, report)
                if report["hasValidLostClaim"] is not True:
                    raise ValueError("The task settled before the kill; discard this missed injection.")
            print(json.dumps({key: report[key] for key in ("documentId", "scopeId", "taskId", "workerService", "reclaimNotBefore", "killRequested")}))
            return
        time.sleep(0.25)
    raise TimeoutError("No unique owned running Fine task was captured; no Worker was changed.")


if __name__ == "__main__":
    main()
