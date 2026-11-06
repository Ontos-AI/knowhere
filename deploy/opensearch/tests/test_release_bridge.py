"""Existing ALB/ECS/private-API drivers form a reversible release sequence."""

from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import httpx
import pytest


def loadBridge() -> type:
    directory: Path = Path(__file__).parents[1]
    sys.path.insert(0, str(directory))
    path: Path = directory / "release_bridge.py"
    assert path.is_file(), "Executable release gate/deploy/smoke bridge is absent"
    specification = importlib.util.spec_from_file_location("release_bridge", path)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return cast(type, module.ReleaseBridge)


class FakeDeployment:
    def __init__(self) -> None:
        self.actions: list[dict[str, object]] = [{"Type": "forward", "TargetGroupArn": "approved-target"}]
        self.definition: str = "old-task"
        self.calls: list[str] = []

    def describe_rules(self, **_values: object) -> dict[str, object]:
        return {"Rules": [{"RuleArn": "approved-rule", "Conditions": [{"Field": "path-pattern", "Values": ["/v*"]}], "Actions": self.actions}]}

    def modify_rule(self, **values: object) -> dict[str, object]:
        self.actions = cast(list[dict[str, object]], values["Actions"])
        self.calls.append("gate-closed" if self.actions[0]["Type"] == "fixed-response" else "gate-open")
        return {}

    def describe_services(self, **_values: object) -> dict[str, object]:
        return {"services": [{"serviceName": "api", "taskDefinition": self.definition, "desiredCount": 1, "runningCount": 1,
            "deployments": [{"status": "PRIMARY", "taskDefinition": self.definition, "rolloutState": "COMPLETED", "pendingCount": 0}]}], "failures": []}

    def describe_task_definition(self, **values: object) -> dict[str, object]:
        return {"taskDefinition": {"taskDefinitionArn": values["taskDefinition"], "containerDefinitions": [{"name": "api", "image": "repo@sha256:" + "a" * 64,
            "environment": [{"name": "KNOWHERE_LEXICAL_BACKEND", "value": "opensearch" if values["taskDefinition"] == "new-task" else "postgresql"}]}]}}

    def update_service(self, **values: object) -> dict[str, object]:
        self.definition = str(values["taskDefinition"])
        self.calls.append("deploy")
        return {}


def createPlan() -> dict[str, object]:
    actions: list[dict[str, object]] = [{"Type": "forward", "TargetGroupArn": "approved-target"}]
    conditions: list[dict[str, object]] = [{"Field": "path-pattern", "Values": ["/v*"]}]
    return {"version": 1, "phase": "forward", "target_backend": "opensearch", "source_hash": "a" * 64,
        "maximum_step_seconds": 30, "restoration_seconds": 30, "margin_seconds": 10,
        "gate": {"rule_arn": "approved-rule", "original_actions": actions,
            "conditions_sha256": sha256(json.dumps(conditions, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "closed_action": {"Type": "fixed-response", "FixedResponseConfig": {"StatusCode": "503", "ContentType": "text/plain", "MessageBody": "Maintenance"}}},
        "deployments": [{"cluster": "existing-cluster", "service": "api", "task_definition": "new-task", "expected_previous_task_definition": "old-task", "container": "api",
            "image": "repo@sha256:" + "a" * 64, "environment": {"KNOWHERE_LEXICAL_BACKEND": "opensearch"}}],
        "smoke": [{"id": "phase", "method": "GET", "path": "/v2/jobs/fixture", "expected_status": 200, "expected_subset": {"status": "done"}}]}


def test_existing_gate_stays_closed_until_control_deployment_and_actual_smoke_match() -> None:
    bridge: type = loadBridge()
    cloud: FakeDeployment = FakeDeployment()
    stages: list[str] = []
    journal: list[dict[str, object]] = []

    def transition(verb: str, _target: str) -> dict[str, object]:
        stages.append(verb)
        assert cloud.actions[0]["Type"] == "fixed-response"
        return {"verb": verb, "errors": [], "execute": True, "sourceHash": "a" * 64,
            "control": {"backend": "opensearch", "mode": "serving" if verb in ("resume", "inspect") else "gated", "epoch": 4},
            "coverage": {"complete": True}, "settledJobs": 0, "backfilledRevisions": 0}

    def respond(_request: httpx.Request) -> httpx.Response:
        assert cloud.actions[0]["Type"] == "fixed-response"
        assert cloud.definition == "new-task"
        assert stages[-1] == "resume"
        return httpx.Response(200, json={"status": "done"})

    with httpx.Client(base_url="http://127.0.0.1:19001", transport=httpx.MockTransport(respond)) as client:
        result = bridge.run(createPlan(), elb=cloud, ecs=cloud, client=client, transition=transition,
            persist=lambda value: journal.append(json.loads(json.dumps(value))))
    assert result["status"] == "serving-smoke-passed"
    assert cloud.calls[0] == "gate-closed" and cloud.calls[-1] == "gate-open"
    assert stages == ["inspect", "gate", "settle", "backfill", "verify", "resume"]
    assert all(record.get("customer_gate") != "open" for record in journal[:-1])


def test_failed_smoke_keeps_existing_customer_gate_closed() -> None:
    bridge: type = loadBridge()
    cloud: FakeDeployment = FakeDeployment()
    with httpx.Client(base_url="http://127.0.0.1:19001", transport=httpx.MockTransport(lambda _request: httpx.Response(503, json={"status": "failed"}))) as client:
        result = bridge.run(createPlan(), elb=cloud, ecs=cloud, client=client,
            transition=lambda verb, _target: {"verb": verb, "errors": [], "execute": True, "sourceHash": "a" * 64,
                "control": {"backend": "opensearch", "mode": "serving" if verb in ("resume", "inspect") else "gated", "epoch": 4},
                "coverage": {"complete": True}, "settledJobs": 0, "backfilledRevisions": 0}, persist=lambda _value: None)
    assert result["status"] == "known-abort"
    assert cloud.actions[0]["Type"] == "fixed-response"
    assert "gate-open" not in cloud.calls


def test_known_abort_rolls_back_to_original_pg_without_resetting_disruption_clock() -> None:
    bridge: type = loadBridge()
    cloud: FakeDeployment = FakeDeployment()

    def transition(verb: str, backend: str) -> dict[str, object]:
        return {"verb": verb, "errors": [], "execute": True, "sourceHash": "a" * 64,
            "control": {"backend": backend, "mode": "serving" if verb in ("resume", "inspect") else "gated", "epoch": 4},
            "coverage": {"complete": True}, "settledJobs": 0, "backfilledRevisions": 0}

    def respond(_request: httpx.Request) -> httpx.Response:
        assert cloud.actions[0]["Type"] == "fixed-response"
        return (httpx.Response(200, json={"status": "done"}) if cloud.definition == "old-task"
            else httpx.Response(503, json={"status": "failed"}))

    with httpx.Client(base_url="http://127.0.0.1:19001", transport=httpx.MockTransport(respond)) as client:
        first = bridge.run(createPlan(), elb=cloud, ecs=cloud, client=client, transition=transition, persist=lambda _value: None)
        assert first["status"] == "known-abort"
        plan: dict[str, object] = createPlan()
        deployment: dict[str, object] = cast(list[dict[str, object]], plan["deployments"])[0]
        deployment.update({"task_definition": "old-task", "expected_previous_task_definition": "new-task",
            "environment": {"KNOWHERE_LEXICAL_BACKEND": "postgresql"}})
        plan.update({"phase": "rollback", "target_backend": "postgresql"})
        final = bridge.run(plan, elb=cloud, ecs=cloud, client=client, transition=transition,
            persist=lambda _value: None, state=first)
    assert final["first_customer_impact_at"] == first["first_customer_impact_at"]
    assert final["status"] == "serving-smoke-passed"
    assert cloud.calls == ["gate-closed", "deploy", "deploy", "gate-open"]


def test_lost_gate_issuance_retains_unknown_and_forbids_rollback() -> None:
    bridge: type = loadBridge()
    journal: list[dict[str, object]] = []

    class LostGateResponse(FakeDeployment):
        def modify_rule(self, **values: object) -> dict[str, object]:
            super().modify_rule(**values)
            raise TimeoutError("Unknown AWS result")

    cloud: LostGateResponse = LostGateResponse()
    with httpx.Client(transport=httpx.MockTransport(lambda _request: pytest.fail("No smoke while gate outcome unknown"))) as client:
        with pytest.raises(TimeoutError):
            bridge.run(createPlan(), elb=cloud, ecs=cloud, client=client,
                transition=lambda *_values: pytest.fail("Unknown gate may not reach SQL"),
                persist=lambda value: journal.append(dict(value)))
        assert journal[-1]["status"] == "unknown"
        plan: dict[str, object] = {**createPlan(), "phase": "rollback", "target_backend": "postgresql"}
        with pytest.raises(ValueError, match="known settled abort"):
            bridge.run(plan, elb=cloud, ecs=cloud, client=client, state=journal[-1],
                transition=lambda *_values: pytest.fail("Unknown gate may not roll back"),
                persist=lambda value: pytest.fail("Unknown gate must stay retained"))
    assert cloud.calls == ["gate-closed"]
