"""The reviewed migration entrypoint is discoverable without database access."""

from pathlib import Path
import subprocess
import sys


def test_transition_command_help_documents_dry_run_and_restore_bindings() -> None:
    command: Path = (
        Path(__file__).resolve().parents[1] / "manage-opensearch-transition.py"
    )
    result = subprocess.run(
        [sys.executable, str(command), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "dry-run" in result.stdout
    for option in (
        "--execute",
        "--epoch",
        "--target-backend",
        "--inventory",
        "--reservation",
        "--host-recovery-receipt",
        "--operation-id",
        "--receipt",
    ):
        assert option in result.stdout
    for verb in (
        "inspect",
        "gate",
        "settle",
        "backfill",
        "verify",
        "resume",
        "abort",
        "reserve-restore",
        "verify-restore",
        "release-restore",
        "reconcile-restart",
    ):
        assert verb in result.stdout


def loadCommand(*, doesUsePhysicalHost=False):
    import importlib.util

    command: Path = (
        Path(__file__).resolve().parents[1] / "manage-opensearch-transition.py"
    )
    specification = importlib.util.spec_from_file_location(
        "kw10_transition_command", command
    )
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    if not doesUsePhysicalHost:
        module._getHostIdentity = lambda: "a" * 64
    return module


def installFakeService(monkeypatch):
    from types import ModuleType

    calls = []

    class FakeTransition:
        def __init__(self, *, sessionFactory):
            pass

        @staticmethod
        def getSourceHash():
            return "reviewed-source"

        def run(self, verb, **kwargs):
            calls.append((verb, kwargs))
            return {
                "version": 1,
                "sourceHash": "reviewed-source",
                "errors": [],
                "execute": kwargs["execute"],
            }

    database = ModuleType("shared.core.database_sync")
    database.get_sync_session_factory = lambda: object()
    transition = ModuleType("shared.services.retrieval.lexical.transition")
    transition.LexicalTransition = FakeTransition
    authority = ModuleType("shared.services.retrieval.lexical.operator_issuer")

    class FakeAuthority:
        def __init__(self, *, sessionFactory):
            pass

        def assertCurrent(self, **kwargs):
            return {"state": "active", **kwargs}

    authority.LexicalOperatorAuthority = FakeAuthority
    monkeypatch.setitem(sys.modules, authority.__name__, authority)
    monkeypatch.setitem(sys.modules, database.__name__, database)
    monkeypatch.setitem(sys.modules, transition.__name__, transition)
    return calls


def test_command_defaults_to_dry_run_and_preserves_existing_receipt(
    monkeypatch, tmp_path, capsys
) -> None:
    calls = installFakeService(monkeypatch)
    command = loadCommand()
    assert command.main(["gate"]) == 0
    assert calls[0][1]["execute"] is False
    receipt = tmp_path / "existing.json"
    receipt.write_text('{"immutable": true}')
    assert (
        command.main(
            [
                "gate",
                "--execute",
                "--epoch",
                "1",
                "--target-backend",
                "opensearch",
                "--receipt",
                str(receipt),
            ]
        )
        == 1
    )
    assert len(calls) == 1
    assert receipt.read_text() == '{"immutable": true}'


def test_command_requires_explicit_target_before_executing_switch(
    monkeypatch, capsys
) -> None:
    calls = installFakeService(monkeypatch)
    command = loadCommand()
    assert command.main(["resume", "--execute", "--epoch", "1"]) == 1
    assert calls == []


def test_changed_checkpoint_source_blocks_mutation(
    monkeypatch, tmp_path, capsys
) -> None:
    calls = installFakeService(monkeypatch)
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"sourceHash": "different", "targetBackend": "opensearch"}')
    command = loadCommand()
    assert (
        command.main(
            [
                "gate",
                "--execute",
                "--target-backend",
                "opensearch",
                "--epoch",
                "1",
                "--checkpoint",
                str(checkpoint),
            ]
        )
        == 1
    )
    assert calls == []


def test_restore_cli_forwards_reviewed_inventory_and_prior_reservation(
    monkeypatch, tmp_path, capsys
) -> None:
    import json
    from types import ModuleType

    installFakeService(monkeypatch)
    captured = []

    class FakeRestore:
        def __init__(self, *, sessionFactory):
            pass

        def run(self, verb, **kwargs):
            captured.append((verb, kwargs))
            return {"mode": verb, "status": "verified", "epoch": 4, "errors": []}

    restore = ModuleType("shared.services.retrieval.lexical.restore_admission")
    restore.LexicalRestoreAdmission = FakeRestore
    monkeypatch.setitem(sys.modules, restore.__name__, restore)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        '{"version":1,"plan":{"writers_fenced":true},"attestation":{"snapshot_uuid":"snapshot"}}'
    )
    reservation = tmp_path / "reservation.json"
    reservation.write_text('{"operation_id":"original-operation"}')
    receipt = tmp_path / "verified.json"
    command = loadCommand()
    assert (
        command.main(
            [
                "verify-restore",
                "--execute",
                "--epoch",
                "4",
                "--operation-id",
                "original-operation",
                "--inventory",
                str(inventory),
                "--reservation",
                str(reservation),
                "--receipt",
                str(receipt),
                *_issuerArguments(),
            ]
        )
        == 0
    )
    assert captured[0][0] == "verify-restore"
    assert captured[0][1]["expectedEpoch"] == 4
    assert captured[0][1]["execute"] is True
    assert captured[0][1]["reservation"] == {"operation_id": "original-operation"}
    output = json.loads(receipt.read_text())
    assert output["sourceHash"] == "reviewed-source" and output["status"] == "verified"


def test_transition_error_receipt_retains_observed_control_and_source(
    monkeypatch, tmp_path, capsys
) -> None:
    import json

    installFakeService(monkeypatch)
    module = sys.modules["shared.services.retrieval.lexical.transition"]

    class FailingTransition:
        def __init__(self, *, sessionFactory):
            pass

        @staticmethod
        def getSourceHash():
            return "reviewed-source"

        def run(self, verb, **kwargs):
            if verb == "inspect":
                return {
                    "control": {"backend": "postgresql", "mode": "gated", "epoch": 2},
                    "currentPublished": {"count": 3},
                    "sourceHash": "reviewed-source",
                }
            raise ValueError("Injected refused activation")

    monkeypatch.setattr(module, "LexicalTransition", FailingTransition)
    receipt = tmp_path / "failure.json"
    command = loadCommand()
    assert (
        command.main(
            [
                "resume",
                "--execute",
                "--epoch",
                "2",
                "--target-backend",
                "opensearch",
                "--receipt",
                str(receipt),
                *_issuerArguments(),
            ]
        )
        == 1
    )
    payload = json.loads(receipt.read_text())
    assert payload["sourceHash"] == "reviewed-source"
    assert (
        payload["control"]["epoch"] == 2 and payload["currentPublished"]["count"] == 3
    )
    assert payload["errors"] == ["Injected refused activation"]
    assert payload["issuer"]["state"] == "active"


def test_changed_reviewed_source_hash_blocks_dispatch_before_session_factory(
    monkeypatch, capsys
) -> None:
    calls = installFakeService(monkeypatch)
    database = sys.modules["shared.core.database_sync"]

    def refuseSessionFactory():
        raise AssertionError(
            "A changed reviewed source reached database initialization"
        )

    monkeypatch.setattr(database, "get_sync_session_factory", refuseSessionFactory)
    command = loadCommand()
    assert (
        command.main(
            [
                "gate",
                "--execute",
                "--epoch",
                "1",
                "--target-backend",
                "opensearch",
                "--expected-source-hash",
                "different-reviewed-source",
            ]
        )
        == 1
    )
    assert calls == []
    assert "reviewed source" in capsys.readouterr().out.lower()


def test_matching_reviewed_source_hash_allows_exact_dispatch(
    monkeypatch, capsys
) -> None:
    calls = installFakeService(monkeypatch)
    command = loadCommand()
    assert (
        command.main(
            [
                "gate",
                "--execute",
                "--epoch",
                "1",
                "--target-backend",
                "opensearch",
                "--expected-source-hash",
                "reviewed-source",
                *_issuerArguments(),
            ]
        )
        == 0
    )
    assert calls[0][0] == "gate" and calls[0][1]["execute"] is True


def _issuerArguments():
    return [
        "--issuer-token",
        "00000000-0000-0000-0000-000000000001",
        "--issuer-host-sha256",
        "a" * 64,
        "--issuer-operation-id",
        "approved-operation",
    ]


def test_mutation_requires_current_physical_issuer_before_dispatch(monkeypatch, capsys):
    calls = installFakeService(monkeypatch)
    command = loadCommand()
    assert (
        command.main(
            ["gate", "--execute", "--epoch", "1", "--target-backend", "opensearch"]
        )
        == 1
    )
    assert calls == []
    assert "issuer" in capsys.readouterr().out.lower()


def test_mutation_rejects_other_host_identity_before_authority_or_dispatch(
    monkeypatch, capsys
):
    calls = installFakeService(monkeypatch)
    command = loadCommand()
    command._getHostIdentity = lambda: "b" * 64
    assert (
        command.main(
            [
                "gate",
                "--execute",
                "--epoch",
                "1",
                "--target-backend",
                "opensearch",
                *_issuerArguments(),
            ]
        )
        == 1
    )
    assert calls == []
    assert "physical" in capsys.readouterr().out.lower()


def test_mutation_echoes_exact_pre_dispatch_authority_receipt(monkeypatch, capsys):
    import json

    calls = installFakeService(monkeypatch)
    command = loadCommand()
    assert (
        command.main(
            [
                "gate",
                "--execute",
                "--epoch",
                "1",
                "--target-backend",
                "opensearch",
                *_issuerArguments(),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert calls[0][0] == "gate"
    assert output["issuer"]["invocationToken"] == "00000000-0000-0000-0000-000000000001"
    assert output["issuer"]["expectedEpoch"] == 1
    assert output["issuer"]["expectedSourceHash"] == "reviewed-source"


def test_authority_denial_never_dispatches_mutation(monkeypatch, capsys):
    calls = installFakeService(monkeypatch)
    command = loadCommand()
    authority = sys.modules[
        "shared.services.retrieval.lexical.operator_issuer"
    ].LexicalOperatorAuthority

    def deny(self, **options):
        raise ValueError("Invocation is fenced")

    monkeypatch.setattr(authority, "assertCurrent", deny)
    assert (
        command.main(
            [
                "gate",
                "--execute",
                "--epoch",
                "1",
                "--target-backend",
                "opensearch",
                *_issuerArguments(),
            ]
        )
        == 1
    )
    assert calls == []
    assert "fenced" in capsys.readouterr().out


def test_physical_issuer_hash_comes_from_actual_stripped_machine_identity(monkeypatch):
    from hashlib import sha256

    command = loadCommand(doesUsePhysicalHost=True)

    def readIdentity(path):
        assert path == Path("/etc/machine-id")
        return b"physical-machine-identity\n"

    monkeypatch.setattr(Path, "read_bytes", readIdentity)
    assert (
        command._getHostIdentity() == sha256(b"physical-machine-identity").hexdigest()
    )
