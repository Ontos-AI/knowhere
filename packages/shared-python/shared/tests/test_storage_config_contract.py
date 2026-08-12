"""Contracts for storage credentials at the boto3 boundary."""

import os
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-bucket")
os.environ.setdefault("S3_ACCESS_KEY_ID", "")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "")
os.environ.setdefault("S3_TEMP_PATH", "/tmp/knowhere-storage-contract")

from shared.core.config.storage import StorageConfig


def create_storage_config(**overrides: str) -> StorageConfig:
    """Create the smallest valid storage configuration for a contract test."""
    values: dict[str, str] = {
        "S3_BUCKET_NAME": "test-bucket",
        "S3_TEMP_PATH": "/tmp/knowhere-storage-contract",
    }
    values.update(overrides)
    return StorageConfig(**values)


def test_aws_s3_uses_default_credential_chain_when_keys_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AWS S3 must allow ECS task-role credentials through boto3's chain."""
    boto3_client: Mock = Mock()
    monkeypatch.setattr("shared.core.config.storage.boto3.client", boto3_client)

    config: StorageConfig = create_storage_config(S3_TYPE="s3", S3_REGION="us-east-1")

    config.get_s3_client()

    boto3_client.assert_called_once()
    client_arguments: dict[str, object] = dict(boto3_client.call_args.kwargs)
    assert client_arguments["service_name"] == "s3"
    assert client_arguments["region_name"] == "us-east-1"
    assert "aws_access_key_id" not in client_arguments
    assert "aws_secret_access_key" not in client_arguments


def test_aws_s3_passes_complete_explicit_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing deployments may continue to provide an explicit key pair."""
    boto3_client: Mock = Mock()
    monkeypatch.setattr("shared.core.config.storage.boto3.client", boto3_client)
    config: StorageConfig = create_storage_config(
        S3_TYPE="s3",
        S3_ACCESS_KEY_ID="access-key",
        S3_SECRET_ACCESS_KEY="secret-key",
    )

    config.get_s3_client()

    client_arguments: dict[str, object] = dict(boto3_client.call_args.kwargs)
    assert client_arguments["aws_access_key_id"] == "access-key"
    assert client_arguments["aws_secret_access_key"] == "secret-key"


@pytest.mark.parametrize("storage_type", ["oss", "minio"])
def test_s3_compatible_backends_require_explicit_credentials(
    storage_type: str,
) -> None:
    """OSS and MinIO must not silently fall back to an AWS identity chain."""
    with pytest.raises(
        ValidationError,
        match=f"Explicit storage credentials are required when S3_TYPE={storage_type}",
    ):
        create_storage_config(S3_TYPE=storage_type)


@pytest.mark.parametrize(
    ("access_key_id", "secret_access_key"),
    [("access-key", ""), ("", "secret-key")],
)
def test_storage_rejects_partial_explicit_credentials(
    access_key_id: str,
    secret_access_key: str,
) -> None:
    """A partial key pair must fail before an unusable client is created."""
    with pytest.raises(
        ValidationError,
        match="S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be configured together",
    ):
        create_storage_config(
            S3_TYPE="s3",
            S3_ACCESS_KEY_ID=access_key_id,
            S3_SECRET_ACCESS_KEY=secret_access_key,
        )
