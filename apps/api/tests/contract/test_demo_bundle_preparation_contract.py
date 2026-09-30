from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_script():
    script_path = (
        Path(__file__).resolve().parents[2] / "scripts" / "prepare_demo_bundles.py"
    )
    spec = importlib.util.spec_from_file_location("prepare_demo_bundles", script_path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load preparation script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preparation_defaults_to_explicit_dry_run(capsys) -> None:
    module = _load_script()

    assert module.main(["demo-spacex-s1"]) == 0
    output = capsys.readouterr().out
    assert "dry-run demo-spacex-s1" in output
    assert "--upload" in output


def test_preparation_requires_source_selection() -> None:
    module = _load_script()

    try:
        module.main([])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("missing source selection should fail")


def test_preparation_rejects_all_with_explicit_source(capsys) -> None:
    module = _load_script()

    try:
        module.main(["--all", "demo-spacex-s1"])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("mixed source selection should fail")
    assert "cannot be combined" in capsys.readouterr().err
