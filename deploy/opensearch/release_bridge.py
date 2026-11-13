"""Existing ALB rule, ECS services and actual private API form a release bridge.

Only existing explicitly reviewed resources are changed. Source-owned transition
commands retain the caller's physical issuer, and a customer gate stays closed
through matching deployment, SQL activation and actual phase/citation smoke.
Unknown cloud/native outcomes never trigger an automatic opposite operation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import re
import time
from types import ModuleType
from typing import Protocol, cast

import httpx

from operations import OpenSearchOperations

__all__: list[str] = ["ReleaseBridge"]


class _ELB(Protocol):
    def describe_rules(self, **values: object) -> dict[str, object]: ...
    def modify_rule(self, **values: object) -> dict[str, object]: ...


class _ECS(Protocol):
    def describe_services(self, **values: object) -> dict[str, object]: ...
    def describe_task_definition(self, **values: object) -> dict[str, object]: ...
    def update_service(self, **values: object) -> dict[str, object]: ...


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Release resource response must be an object")
    return cast(dict[str, object], value)


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _matches(actual: object, expected: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _matches(actual[key], value) for key, value in expected.items())
    return type(actual) is type(expected) and actual == expected


class _KnownAbort(ValueError):
    """All issued mutations settled; a separate reviewed rollback may proceed."""


class ReleaseBridge:
    @staticmethod
    def _rule(client: _ELB, plan: dict[str, object]) -> dict[str, object]:
        rows: list[dict[str, object]] = cast(list[dict[str, object]], client.describe_rules(RuleArns=[plan["rule_arn"]]).get("Rules", []))
        if len(rows) != 1 or rows[0].get("RuleArn") != plan["rule_arn"] or _digest(rows[0].get("Conditions")) != plan["conditions_sha256"]:
            raise ValueError("Existing customer rule identity/conditions differ from the reviewed gate")
        return rows[0]

    @staticmethod
    def _deploy(client: _ECS, plan: dict[str, object], *, target: str, timeoutSeconds: float,
            beforeMutation: Callable[[], None], now: Callable[[], float], sleep: Callable[[float], None]) -> dict[str, object]:
        targetDefinition: dict[str, object] = _object(client.describe_task_definition(taskDefinition=plan["task_definition"])["taskDefinition"])
        containers: list[dict[str, object]] = cast(list[dict[str, object]], targetDefinition.get("containerDefinitions", []))
        selected: list[dict[str, object]] = [container for container in containers if container.get("name") == plan["container"]]
        if (targetDefinition.get("taskDefinitionArn") != plan["task_definition"] or len(selected) != 1
                or selected[0].get("image") != plan["image"]
                or re.fullmatch(r".+@sha256:[a-f0-9]{64}", str(plan["image"])) is None):
            raise ValueError("Deployment requires the reviewed immutable container image")
        environment: dict[str, str] = {row["name"]: row["value"] for row in cast(list[dict[str, str]], selected[0].get("environment", []))}
        if not _matches(environment, plan.get("environment")) or environment.get("KNOWHERE_LEXICAL_BACKEND") != target:
            raise ValueError("API/worker task definition does not bind the selected backend/configuration")
        before: dict[str, object] = client.describe_services(cluster=plan["cluster"], services=[plan["service"]])
        services: list[dict[str, object]] = cast(list[dict[str, object]], before.get("services", []))
        if before.get("failures") or len(services) != 1 or services[0].get("serviceName") != plan["service"]:
            raise ValueError("Deploy only the exact existing approved ECS service")
        previous: object = services[0].get("taskDefinition")
        if previous not in (plan.get("expected_previous_task_definition"), plan["task_definition"]):
            raise _KnownAbort("Existing service task definition differs from the reviewed deployment baseline")
        deadline: float = now() + timeoutSeconds
        if previous != plan["task_definition"]:
            beforeMutation()
            client.update_service(cluster=plan["cluster"], service=plan["service"], taskDefinition=plan["task_definition"], forceNewDeployment=True)
        while now() < deadline:
            response: dict[str, object] = client.describe_services(cluster=plan["cluster"], services=[plan["service"]])
            rows: list[dict[str, object]] = cast(list[dict[str, object]], response.get("services", []))
            if response.get("failures") or len(rows) != 1:
                raise ValueError("Existing service deployment status is uncertain")
            service: dict[str, object] = rows[0]
            deployments: list[dict[str, object]] = cast(list[dict[str, object]], service.get("deployments", []))
            primary: list[dict[str, object]] = [row for row in deployments if row.get("status") == "PRIMARY"]
            if any(row.get("rolloutState") == "FAILED" for row in primary):
                raise ValueError("Existing ECS service deployment failed; keep customer gate closed")
            if (service.get("taskDefinition") == plan["task_definition"] and len(primary) == 1
                    and primary[0].get("rolloutState") == "COMPLETED" and primary[0].get("taskDefinition") == plan["task_definition"]
                    and service.get("runningCount") == service.get("desiredCount") and int(str(service.get("desiredCount", 0))) > 0
                    and int(str(service.get("pendingCount", 0))) == 0
                    and all(row.get("taskDefinition") == plan["task_definition"]
                        or (row.get("runningCount", 0) == 0 and row.get("pendingCount", 0) == 0) for row in deployments)):
                return {"service": plan["service"], "previous_task_definition": previous, "task_definition": plan["task_definition"],
                    "image": plan["image"], "old_finalizers_drained": True, "status": "deployed"}
            sleep(min(1, max(0, deadline - now())))
        raise TimeoutError("ECS deployment outcome remains uncertain; do not restore a different definition automatically")

    @staticmethod
    def smoke(client: httpx.Client, cases: list[dict[str, object]], *, baseDirectory: Path | None = None) -> list[dict[str, object]]:
        if not cases:
            raise ValueError("Release requires explicit actual phase/citation smoke cases")
        results: list[dict[str, object]] = []
        for case in cases:
            method: str = str(case["method"])
            path: str = str(case["path"])
            payload: object = case.get("payload")
            if (method not in ("GET", "POST") or not path.startswith(("/v1/", "/v2/", "/health"))
                    or method == "POST" and not path.endswith(("/retrieval/query", "/indexing/retry"))):
                raise ValueError("Smoke permits explicit readonly queries/phase reads and approved retained-job retry only")
            if path.endswith("/retrieval/query") and (not isinstance(payload, dict) or payload.get("use_agentic") is not False or payload.get("llm_config") is not None):
                raise ValueError("Release lexical smoke must not call a model/provider")
            headers: dict[str, str] = {}
            if case.get("token_file"):
                secret: Path = Path(str(case["token_file"]))
                if not secret.is_file() or secret.stat().st_mode & 0o077 or secret.stat().st_size > 65536:
                    raise ValueError("Smoke credentials require a bounded private token file")
                headers["Authorization"] = "Bearer " + secret.read_text().strip()
            response: httpx.Response = client.request(method, path, json=payload, headers=headers)
            if response.status_code != case["expected_status"] or not _matches(response.json(), case.get("expected_subset", {})):
                raise ValueError("Actual API phase/result smoke did not match its reviewed expectation")
            if case.get("quality_path"):
                qualityPath: Path = (baseDirectory or Path.cwd()) / str(case["quality_path"])
                if sha256(qualityPath.read_bytes()).hexdigest() != case.get("quality_sha256"):
                    raise ValueError("Reviewed smoke evidence file changed")
                modulePath: Path = Path(__file__).resolve().parents[2] / "scripts/probes/run-opensearch-workload.py"
                specification = importlib.util.spec_from_file_location("release_quality", modulePath)
                if specification is None or specification.loader is None:
                    raise ValueError("Public result verifier is unavailable")
                module: ModuleType = importlib.util.module_from_spec(specification)
                specification.loader.exec_module(module)
                quality, identity = module._verifyQuality(_object(response.json()), _object(json.loads(qualityPath.read_bytes())))
                if not quality or not identity:
                    raise ValueError("Actual API content/permission/revision/asset/citation smoke failed")
            results.append({"id": case["id"], "http_status": response.status_code,
                "response_sha256": sha256(response.content).hexdigest(), "status": "passed"})
        return results

    @staticmethod
    def run(plan: dict[str, object], *, elb: _ELB, ecs: _ECS, client: httpx.Client,
            transition: Callable[[str, str], dict[str, object]], persist: Callable[[dict[str, object]], None],
            state: dict[str, object] | None = None, beforeMutation: Callable[[], None] = lambda: None,
            now: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
            baseDirectory: Path | None = None, verifyClosed: Callable[[], dict[str, object]] | None = None) -> dict[str, object]:
        phase: str = str(plan["phase"])
        target: str = str(plan["target_backend"])
        if phase not in ("prepare", "forward", "rollback") or target not in ("postgresql", "opensearch") or phase in ("prepare", "rollback") and target != "postgresql":
            raise ValueError("Prepare/rollback must target compatible PostgreSQL")
        progress: dict[str, object] = dict(state or {})
        isRehearsal: bool = plan.get("rehearsal") is True
        if progress and (phase != "rollback" or (progress.get("status") != "known-abort"
                and not (isRehearsal and progress.get("status") == "rehearsal-forward-gated"))):
            raise ValueError("Only a known settled abort permits reviewed rollback of this disruption journal")
        deadline: float = now() + float(str(plan.get("maximum_workflow_seconds", 600)))
        if deadline <= now():
            raise ValueError("Release requires a bounded workflow duration")
        progress["status"] = "running"
        gate: dict[str, object] = {}
        closed: dict[str, object] = {}
        try:
            if phase != "prepare":
                gate = _object(plan["gate"])
                original: list[dict[str, object]] = cast(list[dict[str, object]], gate["original_actions"])
                if len(original) != 1 or original[0].get("Type") != "forward":
                    raise ValueError("Use an existing simple forward rule; do not rewrite authentication actions")
                rule: dict[str, object] = ReleaseBridge._rule(elb, gate)
                closed = _object(gate["closed_action"])
                if closed.get("Type") != "fixed-response" or _object(closed.get("FixedResponseConfig")).get("StatusCode") != "503":
                    raise ValueError("Reviewed customer gate must explicitly return unavailable while closed")
                if rule.get("Actions") == [closed]:
                    if (not progress.get("first_customer_impact_at") or progress.get("original_actions") != original
                            or progress.get("gate_rule") != gate["rule_arn"]):
                        raise ValueError("An already closed gate requires its original disruption/route intent")
                elif (rule.get("Actions") != original
                        or phase == "rollback" and state and progress.get("customer_gate") != "open"):
                    raise ValueError("Existing rule actions differ; never overwrite an unrelated traffic decision")
                first: str = str(progress.get("first_customer_impact_at") or datetime.now(timezone.utc).isoformat())
                firstTime: datetime = datetime.fromisoformat(first)
                if firstTime.tzinfo is None or firstTime > datetime.now(timezone.utc):
                    raise ValueError("First customer impact must bind the initial gate action")
                budget: dict[str, object] = OpenSearchOperations.planRelease(firstTime,
                    restorationSeconds=int(str(plan["restoration_seconds"])), marginSeconds=int(str(plan["margin_seconds"])))
                if isRehearsal:
                    budget["provisional_for_isolated_rehearsal"] = True
                progress.update({"first_customer_impact_at": first, "original_actions": original,
                    "gate_rule": gate["rule_arn"], "customer_gate": "closing", "abort_plan": budget})
                persist(progress)
                if rule.get("Actions") != [closed]:
                    beforeMutation()
                    elb.modify_rule(RuleArn=gate["rule_arn"], Actions=[closed])
                if ReleaseBridge._rule(elb, gate).get("Actions") != [closed]:
                    raise ValueError("Customer rule closure is uncertain; no deployment/resume is authorized")
                progress["customer_gate"] = "closed"
                persist(progress)
                if verifyClosed is not None:
                    try:
                        progress["public_gate_probe"] = verifyClosed()
                    except (httpx.HTTPError, ValueError) as error:
                        raise _KnownAbort("Public traffic gate closure was not observed") from error
                    persist(progress)

            def remaining() -> float:
                remainingSeconds: float = deadline - now()
                if phase == "forward":
                    cutoff: datetime = datetime.fromisoformat(str(_object(progress["abort_plan"])["latest_abort_at"]))
                    remainingSeconds = min(remainingSeconds, (cutoff - datetime.now(timezone.utc)).total_seconds())
                if remainingSeconds <= 0:
                    raise _KnownAbort("Release budget elapsed; stop forward work and retain the customer gate")
                return remainingSeconds

            def invoke(verb: str, backend: str) -> dict[str, object]:
                remaining()
                receipt: dict[str, object] = transition(verb, backend)
                if receipt.get("verb") != verb or receipt.get("errors") != [] or receipt.get("execute") is not True or receipt.get("sourceHash") != plan["source_hash"]:
                    raise ValueError("Source-owned transition refused or returned another source")
                progress["last_transition"] = receipt
                persist(progress)
                return receipt

            def drain(verb: str, backend: str, count: str) -> None:
                for _round in range(int(str(plan.get("maximum_batches", 1000)))):
                    if int(str(invoke(verb, backend).get(count, 0))) == 0:
                        return
                raise _KnownAbort("Reviewed release batch budget exhausted")
            if phase != "prepare":
                current: dict[str, object] = _object(invoke("inspect", target).get("control"))
                if current.get("mode") == "serving":
                    invoke("gate", target)
                elif current.get("mode") != "gated" or not (phase == "rollback" or plan.get("expected_initial_mode") == "gated"):
                    raise _KnownAbort("Backend barrier differs from the reviewed initial control")
                drain("settle", target, "settledJobs")
            deployed: list[dict[str, object]] = []
            for deployment in cast(list[dict[str, object]], plan["deployments"]):
                deployed.append(ReleaseBridge._deploy(ecs, deployment, target=target,
                    timeoutSeconds=min(remaining(), float(str(plan["maximum_step_seconds"]))), beforeMutation=beforeMutation, now=now, sleep=sleep))
            progress["deployments"] = deployed
            persist(progress)
            coverageTarget: str = "opensearch" if phase == "prepare" else target
            drain("backfill", coverageTarget, "backfilledRevisions")
            verified: dict[str, object] = invoke("verify", coverageTarget)
            if _object(verified.get("coverage")).get("complete") is not True:
                raise _KnownAbort("Current published target coverage is incomplete")
            if phase == "prepare":
                progress.update({"status": "PG-compatible-shadow-prepared", "customer_gate": "unchanged", "service_restored": False})
                persist(progress)
                return progress
            resumed: dict[str, object] = invoke("resume", target)
            control: dict[str, object] = _object(resumed.get("control"))
            if control.get("mode") != "serving" or control.get("backend") != target:
                raise ValueError("Actual control did not select the intended serving backend")
            if target == "opensearch":
                # Resume retires every manifest. Seeding writes each namespace's current
                # build list so eligible reads use manifests from the first request.
                drain("seed-manifests", target, "seededNamespaces")
            remaining()
            smoke: list[dict[str, object]] = cast(list[dict[str, object]], plan["smoke"])
            if any(str(case["path"]).endswith("/indexing/retry") for case in smoke):
                raise _KnownAbort("Gate verification smoke must not mutate a retained indexing job")
            try:
                progress["smoke"] = ReleaseBridge.smoke(client, smoke, baseDirectory=baseDirectory)
            except (httpx.HTTPError, ValueError) as error:
                raise _KnownAbort("Actual private API smoke failed; retain the gate for reviewed rollback") from error
            persist(progress)
            remaining()
            if isRehearsal and phase == "forward":
                progress.update({"status": "rehearsal-forward-gated", "customer_gate": "closed",
                    "service_restored": False, "requires_explicit_compatible_rollback": True})
                persist(progress)
                return progress
            if ReleaseBridge._rule(elb, gate).get("Actions") != [closed]:
                raise ValueError("Customer gate changed during release; do not overwrite its new owner")
            progress["customer_gate"] = "opening"
            persist(progress)
            beforeMutation()
            elb.modify_rule(RuleArn=gate["rule_arn"], Actions=gate["original_actions"])
            if ReleaseBridge._rule(elb, gate).get("Actions") != gate["original_actions"]:
                raise ValueError("Customer gate reopening is uncertain")
            progress.update({"status": "serving-smoke-passed", "customer_gate": "open",
                "service_restored": False, "public_edge_probe_required": True})
            persist(progress)
            return progress
        except _KnownAbort as error:
            progress.update({"status": "known-abort", "service_restored": False, "reason": str(error)})
            persist(progress)
            return progress
        except BaseException:
            progress["status"] = "unknown"
            persist(progress)
            raise
