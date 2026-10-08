"""Manually restore verified demo originals through the v2 HTTP jobs API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class RestoreRequest:
    api_url: str
    api_key: str
    backup_directory: Path
    state_directory: Path
    timeout_seconds: float
    can_reparse_ready: bool
    can_retry_failed: bool


def write_state(path: Path, state: dict[str, Any]) -> None:
    temporary: Path = path.with_suffix(".partial")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(path)


def validate_backup(entry: dict[str, Any], directory: Path) -> Path:
    path: Path = (directory / str(entry["relative_path"])).resolve()
    if (
        not path.is_relative_to(directory.resolve())
        or entry.get("status") != "verified"
    ):
        raise ValueError("Original is outside the verified backup inventory")
    if not isinstance(entry.get("validation"), dict) or not entry["validation"].get(
        "is_valid"
    ):
        raise ValueError("Original format validation is missing")
    digest = hashlib.sha256()
    with path.open("rb") as original:
        for block in iter(lambda: original.read(1024 * 1024), b""):
            digest.update(block)
    if (
        path.stat().st_size != entry["size_bytes"]
        or digest.hexdigest() != entry["sha256"]
    ):
        raise ValueError("Original size or SHA-256 differs from the verified inventory")
    return path


def request_json(
    client: httpx.Client, method: str, path: str, **arguments: Any
) -> dict[str, Any]:
    response: httpx.Response | None = None
    for attempt in range(4):
        try:
            response = client.request(method, path, **arguments)
            break
        except httpx.TransportError:
            if method != "GET" or attempt == 3:
                raise
            time.sleep(min(2**attempt, 8))
    if response is None:
        raise RuntimeError("API request attempts completed without a response")
    if response.is_error:
        # Never persist response bodies, credentials, storage keys or signed URLs.
        try:
            errorCode: str = str(
                response.json().get("error", {}).get("code", "HTTP_ERROR")
            )
        except (ValueError, AttributeError):
            errorCode = "HTTP_ERROR"
        raise RuntimeError(f"API returned {response.status_code} {errorCode}")
    payload: object = response.json()
    if not isinstance(payload, dict):
        raise ValueError("API response must be an object")
    return payload


def restore_source(entry: dict[str, Any], request: RestoreRequest) -> dict[str, Any]:
    sourceId: str = str(entry["source_id"])
    statePath: Path = request.state_directory / (
        hashlib.sha256(sourceId.encode()).hexdigest() + ".json"
    )
    state: dict[str, Any] = (
        json.loads(statePath.read_text())
        if statePath.exists()
        else {"source_id": sourceId}
    )
    startedAt: float = time.monotonic()
    try:
        originalPath: Path = validate_backup(entry, request.backup_directory)
        with httpx.Client(
            base_url=request.api_url.rstrip("/") + "/",
            headers={"Authorization": "Bearer " + request.api_key},
            timeout=60,
        ) as client:
            job: dict[str, Any] | None = None
            if state.get("job_id"):
                job = request_json(client, "GET", "api/v2/jobs/" + state["job_id"])
                if (
                    job.get("namespace") != "__knowhere_demo__"
                    or job.get("data_id") != sourceId
                    or state.get("sha256") != entry["sha256"]
                ):
                    raise ValueError(
                        "Recorded job does not match this source and verified original"
                    )
                if job.get("status") == "done" and request.can_reparse_ready:
                    state["previous_job_ids"] = [
                        *state.get("previous_job_ids", []),
                        state.pop("job_id"),
                    ]
                    job = None
                if job is not None and job.get("status") == "failed":
                    if not request.can_retry_failed:
                        raise RuntimeError(
                            "Prior job failed; use --retry-failed to create another revision"
                        )
                    state["previous_job_ids"] = [
                        *state.get("previous_job_ids", []),
                        state.pop("job_id"),
                    ]
                    job = None
            if job is None:
                createStarted: float = time.monotonic()
                job = request_json(
                    client,
                    "POST",
                    "api/v2/jobs",
                    json={
                        "namespace": "__knowhere_demo__",
                        "source_type": "file",
                        "file_name": entry["file_name"],
                        "data_id": sourceId,
                    },
                )
                state.update(
                    {
                        "job_id": job["job_id"],
                        "document_id": job["document_id"],
                        "status": "waiting-file",
                        "sha256": entry["sha256"],
                        "create_seconds": round(time.monotonic() - createStarted, 3),
                    }
                )
                write_state(statePath, state)
            if job.get("status") == "waiting-file" or job.get("upload_url"):
                if not job.get("upload_url"):
                    job.update(
                        request_json(
                            client,
                            "POST",
                            f"api/v2/jobs/{state['job_id']}/upload-url",
                            json={},
                        )
                    )
                uploadUrl: str = str(job.get("upload_url") or "")
                if not uploadUrl:
                    raise RuntimeError(
                        "Waiting job has no upload URL; inspect it before resuming"
                    )
                uploadHeaders: dict[str, str] = job.get("upload_headers") or {
                    "Content-Type": "application/pdf"
                    if originalPath.suffix.lower() == ".pdf"
                    else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                }
                # Use a separate client so the API bearer never reaches storage.
                uploadStarted: float = time.monotonic()
                with (
                    originalPath.open("rb") as original,
                    httpx.Client(timeout=600) as storageClient,
                ):
                    uploaded: httpx.Response = storageClient.put(
                        uploadUrl,
                        content=original,
                        headers={
                            **uploadHeaders,
                            "Content-Length": str(originalPath.stat().st_size),
                        },
                    )
                    uploaded.raise_for_status()
                state["upload_seconds"] = round(time.monotonic() - uploadStarted, 3)
                confirmStarted: float = time.monotonic()
                request_json(
                    client,
                    "POST",
                    f"api/v2/jobs/{state['job_id']}/confirm-upload",
                    json={},
                )
                state["confirm_seconds"] = round(time.monotonic() - confirmStarted, 3)
                write_state(statePath, state)
            interval: float = 2
            while True:
                job = request_json(client, "GET", f"api/v2/jobs/{state['job_id']}")
                state["status"] = str(job.get("status"))
                write_state(statePath, state)
                if state["status"] in ("done", "failed"):
                    break
                if time.monotonic() - startedAt > request.timeout_seconds:
                    raise TimeoutError(
                        "Polling deadline reached; job is retained for --resume"
                    )
                time.sleep(interval)
                interval = min(interval * 1.5, 30)
            if state["status"] != "done":
                raise RuntimeError(
                    "Parser job failed; inspect the authorized job response"
                )
            catalog: dict[str, Any] = request_json(client, "GET", "api/v1/demo/catalog")
            source: dict[str, Any] | None = next(
                (
                    item
                    for item in catalog["sources"]
                    if item["demo_source_id"] == sourceId
                ),
                None,
            )
            if (
                source is None
                or source["canonical_document_id"] != state["document_id"]
            ):
                raise RuntimeError("Completed job is absent from the READY catalog")
            revisionId: str = str(job.get("job_result_id") or "")
            if not revisionId:
                raise RuntimeError(
                    "Completed job did not return its immutable revision"
                )
            chunks: dict[str, Any] = request_json(
                client,
                "GET",
                f"api/v2/documents/{state['document_id']}/chunks",
                params={"job_result_id": revisionId, "page_size": 1},
            )
            state.update(
                {
                    "status": "ready",
                    "job_result_id": revisionId,
                    "chunk_count": chunks["pagination"]["total"],
                    "elapsed_seconds": round(time.monotonic() - startedAt, 3),
                }
            )
            state.pop("error_type", None)
    except Exception as error:
        state["error_type"] = type(error).__name__
        state["status"] = "restore_failed"
        print(f"{sourceId}: failed ({type(error).__name__})", flush=True)
    else:
        print(f"{sourceId}: ready ({state['chunk_count']} chunks)", flush=True)
    write_state(statePath, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True, help="Origin URL, without /api")
    parser.add_argument("--api-key-env", default="KNOWHERE_DEMO_API_KEY")
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument(
        "--source",
        action="append",
        help="Source ID; repeat to select several, omit for the full inventory",
    )
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=2, choices=range(1, 9))
    parser.add_argument("--timeout-seconds", type=float, default=14400)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse recorded jobs (always on; provided for explicit operator intent)",
    )
    parser.add_argument(
        "--reparse-ready",
        action="store_true",
        help="Create a new revision for already completed recorded jobs",
    )
    parser.add_argument("--retry-failed", action="store_true")
    arguments: argparse.Namespace = parser.parse_args()
    apiKey: str = os.environ.get(arguments.api_key_env, "")
    if not apiKey:
        parser.error("Maintainer API key environment variable is empty")
    inventory: list[dict[str, Any]] = json.loads(arguments.inventory.read_text())
    if any(entry.get("status") != "verified" for entry in inventory):
        parser.error("Backup inventory is incomplete; restoration is blocked")
    sources: set[str] = set(arguments.source or [])
    if sources - {entry["source_id"] for entry in inventory}:
        parser.error("Requested source is absent from the inventory")
    selected: list[dict[str, Any]] = [
        entry for entry in inventory if not sources or entry["source_id"] in sources
    ]
    arguments.state_directory.mkdir(parents=True, exist_ok=True)
    request: RestoreRequest = RestoreRequest(
        arguments.api_url,
        apiKey,
        arguments.inventory.parent,
        arguments.state_directory,
        arguments.timeout_seconds,
        arguments.reparse_ready,
        arguments.retry_failed,
    )
    with ThreadPoolExecutor(max_workers=arguments.concurrency) as executor:
        results: list[dict[str, Any]] = list(
            executor.map(lambda entry: restore_source(entry, request), selected)
        )
    write_state(arguments.state_directory / "report.json", {"sources": results})
    return 0 if all(result["status"] == "ready" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
