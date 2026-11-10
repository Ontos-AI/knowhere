"""The SDK campaign requires explicit artifacts and records its isolated run."""
from pathlib import Path
import subprocess
import sys


def test_sdk_http_campaign_help_exposes_artifact_bindings() -> None:
    command = Path(__file__).resolve().parents[1] / "verify-sdk-http-contract.py"
    result = subprocess.run([sys.executable, str(command), "--help"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    for option in ("--node-sdk-root", "--python-sdk-root", "--artifact-dir", "--python-sdk-wheel"):
        assert option in result.stdout
