"""Configure only the disposable stack's real S3 -> SNS -> API delivery path."""

from __future__ import annotations

import argparse
import copy
import json
import time
from urllib.parse import urlparse
from urllib.request import urlopen

import boto3
from botocore.client import BaseClient
from botocore.config import Config

REGION: str = "us-west-1"
ACCOUNT_ID: str = "000000000000"
UPLOAD_BUCKET: str = "knowhere-progressive-e2e-uploads"
TOPIC_NAME: str = "knowhere-progressive-e2e-upload-events"
TOPIC_ARN: str = f"arn:aws:sns:{REGION}:{ACCOUNT_ID}:{TOPIC_NAME}"
NOTIFICATION_ID: str = "knowhere-progressive-e2e-upload-created"
API_ORIGIN: str = "http://knowhere-api:5005"
# main.py root_path=/api; router registry adds /v1 and /internal.
CALLBACK_ENDPOINT: str = f"{API_ORIGIN}/api/v1/internal/s3-events"
TIMEOUT_SECONDS: int = 60


def create_client(service: str, endpoint: str) -> BaseClient:
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "localstack", "storage"}
        or parsed.port != 4566
        or parsed.username is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Storage notification setup requires an isolated LocalStack endpoint"
        )
    return boto3.client(
        service,
        endpoint_url=endpoint,
        region_name=REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2}),
    )


def read_notification_configuration(client: BaseClient) -> dict[str, object]:
    response: dict[str, object] = client.get_bucket_notification_configuration(
        Bucket=UPLOAD_BUCKET
    )
    allowed: set[str] = {
        "TopicConfigurations",
        "QueueConfigurations",
        "LambdaFunctionConfigurations",
        "EventBridgeConfiguration",
    }
    return {key: value for key, value in response.items() if key in allowed}


def expected_notification() -> dict[str, object]:
    return {
        "Id": NOTIFICATION_ID,
        "TopicArn": TOPIC_ARN,
        "Events": ["s3:ObjectCreated:*"],
        "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "uploads/"}]}},
    }


def configure_storage(endpoint: str) -> None:
    notification: BaseClient = create_client("sns", endpoint)
    storage: BaseClient = create_client("s3", endpoint)
    created: dict[str, object] = notification.create_topic(Name=TOPIC_NAME)
    if created.get("TopicArn") != TOPIC_ARN:
        raise RuntimeError(
            "Refusing to configure a topic outside the expected test account/region"
        )
    policy: dict[str, object] = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowOnlyOwnedTestUploadBucket",
                "Effect": "Allow",
                "Principal": {"Service": "s3.amazonaws.com"},
                "Action": "sns:Publish",
                "Resource": TOPIC_ARN,
                "Condition": {
                    "ArnEquals": {"aws:SourceArn": f"arn:aws:s3:::{UPLOAD_BUCKET}"},
                    "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                },
            }
        ],
    }
    notification.set_topic_attributes(
        TopicArn=TOPIC_ARN, AttributeName="Policy", AttributeValue=json.dumps(policy)
    )
    configuration: dict[str, object] = read_notification_configuration(storage)
    existing: object = configuration.get("TopicConfigurations", [])
    if not isinstance(existing, list):
        raise RuntimeError("Unexpected test bucket topic configuration")
    retained: list[object] = [
        item
        for item in existing
        if isinstance(item, dict) and item.get("Id") != NOTIFICATION_ID
    ]
    configuration["TopicConfigurations"] = [*retained, expected_notification()]
    storage.put_bucket_notification_configuration(
        Bucket=UPLOAD_BUCKET, NotificationConfiguration=configuration
    )
    verify_notification(storage)
    print(
        json.dumps(
            {
                "phase": "configure",
                "bucket": UPLOAD_BUCKET,
                "topic_arn": TOPIC_ARN,
                "prefix": "uploads/",
                "events": ["s3:ObjectCreated:*"],
            }
        ),
        flush=True,
    )


def verify_notification(storage: BaseClient) -> None:
    configuration: dict[str, object] = read_notification_configuration(storage)
    topics: object = configuration.get("TopicConfigurations", [])
    if not isinstance(topics, list):
        raise RuntimeError("Test upload bucket has no topic configuration")
    owned: list[object] = [
        item
        for item in topics
        if isinstance(item, dict) and item.get("Id") == NOTIFICATION_ID
    ]
    normalized: list[dict[str, object]] = [
        normalize_notification(item) for item in owned if isinstance(item, dict)
    ]
    if normalized != [normalize_notification(expected_notification())]:
        raise RuntimeError(
            "Owned upload bucket notification does not match the required topic/prefix/events"
        )


def normalize_notification(notification: dict[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = copy.deepcopy(notification)
    events: object = normalized.get("Events")
    if isinstance(events, list) and all(isinstance(event, str) for event in events):
        normalized["Events"] = sorted(events)
    filtering: object = normalized.get("Filter")
    if not isinstance(filtering, dict):
        return normalized
    key: object = filtering.get("Key")
    if not isinstance(key, dict):
        return normalized
    rules: object = key.get("FilterRules")
    if not isinstance(rules, list):
        return normalized
    for rule in rules:
        if isinstance(rule, dict) and isinstance(rule.get("Name"), str):
            rule["Name"] = rule["Name"].lower()
    key["FilterRules"] = sorted(
        rules, key=lambda rule: json.dumps(rule, sort_keys=True)
    )
    return normalized


def wait_api_ready() -> None:
    deadline: float = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{API_ORIGIN}/health", timeout=5) as response:
                if response.status == 200:
                    # GET verifies the actual route without injecting a storage event.
                    with urlopen(CALLBACK_ENDPOINT, timeout=5) as callback:
                        if callback.status == 200:
                            return
        except OSError:
            # The disposable API may still be starting; retry until the deadline.
            pass
        time.sleep(1)
    raise RuntimeError(
        "API health or the real /api/v1/internal/s3-events route is unavailable"
    )


def read_subscriptions(client: BaseClient) -> list[dict[str, object]]:
    subscriptions: list[dict[str, object]] = []
    token: str | None = None
    while True:
        arguments: dict[str, str] = {"TopicArn": TOPIC_ARN}
        if token:
            arguments["NextToken"] = token
        response: dict[str, object] = client.list_subscriptions_by_topic(**arguments)
        rows: object = response.get("Subscriptions", [])
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected test SNS subscription response")
        subscriptions.extend(
            item
            for item in rows
            if isinstance(item, dict)
            and item.get("TopicArn") == TOPIC_ARN
            and item.get("Protocol") == "http"
            and item.get("Endpoint") == CALLBACK_ENDPOINT
        )
        token = str(response["NextToken"]) if response.get("NextToken") else None
        if token is None:
            return subscriptions


def subscribe_api(endpoint: str) -> None:
    wait_api_ready()
    notification: BaseClient = create_client("sns", endpoint)
    verify_notification(create_client("s3", endpoint))
    existing: list[dict[str, object]] = read_subscriptions(notification)
    has_existing_subscription: bool = bool(existing)
    if len(existing) > 1:
        raise RuntimeError(
            "Duplicate owned SNS subscriptions exist; refusing to add another"
        )
    if not existing:
        notification.subscribe(
            TopicArn=TOPIC_ARN,
            Protocol="http",
            Endpoint=CALLBACK_ENDPOINT,
            ReturnSubscriptionArn=True,
            Attributes={"RawMessageDelivery": "false"},
        )
    deadline: float = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        existing = read_subscriptions(notification)
        if len(existing) > 1:
            raise RuntimeError("Duplicate owned SNS subscriptions appeared")
        if len(existing) == 1:
            subscription_arn: str = str(existing[0].get("SubscriptionArn") or "")
            if subscription_arn.startswith(f"{TOPIC_ARN}:"):
                attributes: dict[str, object] = (
                    notification.get_subscription_attributes(
                        SubscriptionArn=subscription_arn
                    ).get("Attributes", {})
                )
                if (
                    str(attributes.get("PendingConfirmation", "false")).lower()
                    == "false"
                    and attributes.get("Endpoint") == CALLBACK_ENDPOINT
                    and attributes.get("TopicArn") == TOPIC_ARN
                ):
                    print(
                        json.dumps(
                            {
                                "phase": "subscribe",
                                "subscription_count": 1,
                                "confirmed": True,
                                "topic_arn": TOPIC_ARN,
                                "endpoint": CALLBACK_ENDPOINT,
                                "reused_subscription": has_existing_subscription,
                            }
                        ),
                        flush=True,
                    )
                    return
        time.sleep(1)
    raise RuntimeError(
        "SNS subscription was not confirmed by the real API; inspect LocalStack alias/LOCALSTACK_HOST and API confirmation logs. No duplicate subscription was created"
    )


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("configure", "subscribe"), required=True)
    parser.add_argument("--storage-endpoint", default="http://storage:4566")
    arguments: argparse.Namespace = parser.parse_args()
    if arguments.phase == "configure":
        configure_storage(arguments.storage_endpoint)
        return
    subscribe_api(arguments.storage_endpoint)


if __name__ == "__main__":
    main()
