"""Render staging ECS task-definition templates without storing secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Final


PLACEHOLDER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\$\{([A-Z0-9_]+)\}")
REQUIRED_VARIABLES: Final[tuple[str, ...]] = (
    "API_IMAGE",
    "WORKER_IMAGE",
    "EXECUTION_ROLE_ARN",
    "API_TASK_ROLE_ARN",
    "WORKER_TASK_ROLE_ARN",
    "STAGING_SECRETS_ARN",
)
FORBIDDEN_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    {"S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"}
)


def replace_placeholders(value: object, variables: dict[str, str]) -> object:
    """Recursively replace required placeholders in a JSON-compatible value."""
    if isinstance(value, str):
        return PLACEHOLDER_PATTERN.sub(
            lambda match: variables.get(match.group(1), match.group(0)), value
        )
    if isinstance(value, list):
        return [replace_placeholders(item, variables) for item in value]
    if isinstance(value, dict):
        return {
            str(key): replace_placeholders(item, variables)
            for key, item in value.items()
        }
    return value


def collect_strings(value: object) -> list[str]:
    """Collect all string values for unresolved-placeholder validation."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in collect_strings(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in collect_strings(child)]
    return []


def validate_rendered_definition(definition: object) -> None:
    """Reject unresolved placeholders and long-lived S3 credential variables."""
    strings = collect_strings(definition)
    unresolved = [value for value in strings if PLACEHOLDER_PATTERN.search(value)]
    if unresolved:
        raise ValueError(f"Unresolved task-definition placeholders: {unresolved}")

    containers = definition.get("containerDefinitions") if isinstance(definition, dict) else None
    if not isinstance(containers, list):
        raise ValueError("Task definition must contain containerDefinitions")
    environment_names = {
        str(item.get("name"))
        for container in containers
        if isinstance(container, dict)
        for item in [*(container.get("environment") or []), *(container.get("secrets") or [])]
        if isinstance(item, dict)
    }
    forbidden = environment_names & FORBIDDEN_ENVIRONMENT_NAMES
    if forbidden:
        raise ValueError(f"Task definition contains forbidden S3 credentials: {sorted(forbidden)}")


def render_template(template_path: Path, output_path: Path, variables: dict[str, str]) -> None:
    """Render one JSON template to a deployment-only output path."""
    definition = json.loads(template_path.read_text(encoding="utf-8"))
    rendered = replace_placeholders(definition, variables)
    validate_rendered_definition(rendered)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(rendered, indent=2) + "\n", encoding="utf-8")


def parse_arguments() -> argparse.Namespace:
    """Parse renderer CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def load_variables() -> dict[str, str]:
    """Load and validate deployment inputs from the environment."""
    variables = {name: os.environ.get(name, "") for name in REQUIRED_VARIABLES}
    missing = [name for name, value in variables.items() if not value]
    if missing:
        raise ValueError(f"Missing required renderer variables: {', '.join(missing)}")
    return variables


def main() -> None:
    """Render both staging task definitions."""
    arguments = parse_arguments()
    variables = load_variables()
    template_directory = Path(__file__).parent
    render_template(
        template_directory / "task-definition-api.staging.json",
        arguments.output_dir / "knowhere-api-staging.json",
        variables,
    )
    render_template(
        template_directory / "task-definition-worker.staging.json",
        arguments.output_dir / "knowhere-worker-staging.json",
        variables,
    )


if __name__ == "__main__":
    main()
