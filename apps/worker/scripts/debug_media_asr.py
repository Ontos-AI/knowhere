#!/usr/bin/env python3
"""P0 spike: call DashScope Fun-ASR file transcription on real media URLs.

Confirms sentence timestamps, diarization (`speaker_id`), long-file limits,
latency, and that the async API accepts a file URL.

Usage:
  cd apps/worker
  uv run python scripts/debug_media_asr.py \\
    --file-url https://example.com/podcast.mp3 \\
    --file-url https://example.com/meeting.wav \\
    --file-url https://example.com/lecture.mp4
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from loguru import logger

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"))

# DashScope SDK `jobs.wait` defaults (not product thresholds).
_POLL_INTERVAL_SECONDS = 1.0
_MAX_POLL_INTERVAL_SECONDS = 8.0
_FUN_ASR_MODEL = "fun-asr"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="P0 Fun-ASR spike: timestamps, diarization, URL input, latency"
    )
    parser.add_argument(
        "--file-url",
        action="append",
        dest="file_urls",
        required=True,
        help="Public HTTPS URL of one audio/video file. Repeat for each sample.",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Optional Fun-ASR language hint, for example zh or en.",
    )
    parser.add_argument(
        "--speaker-count",
        type=int,
        default=None,
        help="Optional speaker-count hint passed to Fun-ASR diarization.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write the JSON report.",
    )
    args = parser.parse_args()

    from shared.core.config import settings

    api_key = _first_ali_api_key()
    api_root = _dashscope_api_v1_root(settings.ALI_URL)
    reports: list[dict[str, Any]] = []
    for file_url in args.file_urls:
        reports.append(
            _transcribe_file_url(
                api_root=api_root,
                api_key=api_key,
                file_url=file_url,
                language_hint=args.language,
                speaker_count=args.speaker_count,
            )
        )
    report = {"api_root": api_root, "model": _FUN_ASR_MODEL, "files": reports}
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.write("\n")
        logger.info(f"Wrote Fun-ASR spike report: {args.output}")
    return 0 if all(item.get("ok") for item in reports) else 1


def _first_ali_api_key() -> str:
    from shared.core.config import settings
    from shared.services.quota.token_pool import BaseQuotaManager

    tokens = BaseQuotaManager.parse_token_specs(
        (settings.ALI_API_KEYS or "").strip(),
        default_rpm_limit=settings.ALI_TOKEN_RPM_LIMIT,
        default_daily_limit=settings.ALI_TOKEN_DAILY_LIMIT,
    )
    if not tokens:
        raise SystemExit(
            "ALI_API_KEYS is empty. Set it in apps/worker/.env before the P0 spike."
        )
    return tokens[0].api_key


def _dashscope_api_v1_root(ali_url: str) -> str:
    parsed = urlparse(ali_url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit(f"ALI_URL is not a valid DashScope URL: {ali_url}")
    return f"{parsed.scheme}://{parsed.netloc}/api/v1"


def _transcribe_file_url(
    *,
    api_root: str,
    api_key: str,
    file_url: str,
    language_hint: str | None,
    speaker_count: int | None,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {"diarization_enabled": True}
    if language_hint:
        parameters["language_hints"] = [language_hint]
    if speaker_count is not None:
        parameters["speaker_count"] = speaker_count
    started = time.perf_counter()
    try:
        with httpx.Client() as client:
            task_id = _submit_transcription(
                client,
                api_root=api_root,
                api_key=api_key,
                file_url=file_url,
                parameters=parameters,
            )
            job = _wait_for_task(
                client,
                api_root=api_root,
                api_key=api_key,
                task_id=task_id,
            )
            transcripts = _download_transcripts(client, job)
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.exception(f"Fun-ASR spike failed for {file_url}")
        return {
            "ok": False,
            "file_url": file_url,
            "latency_ms": latency_ms,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    latency_ms = int((time.perf_counter() - started) * 1000)
    sentences = _sentences_from_transcripts(transcripts)
    speakers = sorted(
        {
            str(sentence["speaker_id"])
            for sentence in sentences
            if sentence.get("speaker_id") is not None
        }
    )
    return {
        "ok": True,
        "file_url": file_url,
        "task_id": job.get("task_id") or job.get("output", {}).get("task_id"),
        "task_status": job.get("output", {}).get("task_status") or job.get("task_status"),
        "latency_ms": latency_ms,
        "sentence_count": len(sentences),
        "speakers": speakers,
        "has_sentence_timestamps": all(
            isinstance(sentence.get("begin_time"), int)
            and isinstance(sentence.get("end_time"), int)
            for sentence in sentences
        )
        if sentences
        else False,
        "has_diarization": bool(speakers),
        "content_duration_ms": _first_content_duration_ms(transcripts),
        "original_duration_ms": _first_original_duration_ms(transcripts),
        "sentences": sentences,
        "job": job,
        "transcripts": transcripts,
    }


def _submit_transcription(
    client: httpx.Client,
    *,
    api_root: str,
    api_key: str,
    file_url: str,
    parameters: dict[str, Any],
) -> str:
    response = client.post(
        f"{api_root}/services/audio/asr/transcription",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        },
        json={
            "model": _FUN_ASR_MODEL,
            "input": {"file_urls": [file_url]},
            "parameters": parameters,
        },
    )
    payload = _json_or_text(response)
    if response.status_code >= 400:
        raise RuntimeError(
            f"Fun-ASR submit failed: status={response.status_code} body={payload}"
        )
    task_id = _task_id_from_submit(payload)
    if not task_id:
        raise RuntimeError(f"Fun-ASR submit returned no task_id: {payload}")
    logger.info(f"Submitted Fun-ASR task {task_id} for {file_url}")
    return task_id


def _wait_for_task(
    client: httpx.Client,
    *,
    api_root: str,
    api_key: str,
    task_id: str,
) -> dict[str, Any]:
    interval = _POLL_INTERVAL_SECONDS
    while True:
        response = client.get(
            f"{api_root}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        payload = _json_or_text(response)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Fun-ASR poll failed: status={response.status_code} body={payload}"
            )
        if not isinstance(payload, dict):
            raise RuntimeError(f"Fun-ASR poll returned a non-object: {payload}")
        status = str(
            payload.get("output", {}).get("task_status")
            or payload.get("task_status")
            or ""
        ).upper()
        logger.info(f"Fun-ASR task {task_id} status={status}")
        if status in {"SUCCEEDED", "FAILED"}:
            if status == "FAILED":
                raise RuntimeError(f"Fun-ASR task failed: {payload}")
            return payload
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                interval = max(float(retry_after), _POLL_INTERVAL_SECONDS)
            except ValueError:
                interval = min(interval * 2, _MAX_POLL_INTERVAL_SECONDS)
        time.sleep(interval)
        interval = min(interval * 2, _MAX_POLL_INTERVAL_SECONDS)


def _download_transcripts(
    client: httpx.Client,
    job: dict[str, Any],
) -> list[dict[str, Any]]:
    results = job.get("output", {}).get("results") or job.get("results") or []
    transcripts: list[dict[str, Any]] = []
    if not isinstance(results, list):
        return transcripts
    for result in results:
        if not isinstance(result, dict):
            continue
        transcription_url = result.get("transcription_url")
        if not isinstance(transcription_url, str) or not transcription_url:
            continue
        response = client.get(transcription_url)
        payload = _json_or_text(response)
        if response.status_code >= 400:
            raise RuntimeError(
                "Fun-ASR transcript download failed: "
                f"status={response.status_code} body={payload}"
            )
        if isinstance(payload, dict):
            transcripts.append(payload)
    return transcripts


def _sentences_from_transcripts(transcripts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sentences: list[dict[str, Any]] = []
    for transcript in transcripts:
        transcripts_block = transcript.get("transcripts")
        blocks: list[Any]
        if isinstance(transcripts_block, list):
            blocks = transcripts_block
        else:
            blocks = [transcript]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            raw_sentences = block.get("sentences")
            if not isinstance(raw_sentences, list):
                continue
            for sentence in raw_sentences:
                if not isinstance(sentence, dict):
                    continue
                sentences.append(
                    {
                        "begin_time": sentence.get("begin_time"),
                        "end_time": sentence.get("end_time"),
                        "text": sentence.get("text"),
                        "speaker_id": sentence.get("speaker_id"),
                    }
                )
    return sentences


def _first_content_duration_ms(transcripts: list[dict[str, Any]]) -> int | None:
    for transcript in transcripts:
        value = transcript.get("content_duration")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        transcripts_block = transcript.get("transcripts")
        if isinstance(transcripts_block, list):
            for block in transcripts_block:
                if isinstance(block, dict):
                    nested = block.get("content_duration")
                    if isinstance(nested, int) and not isinstance(nested, bool):
                        return nested
    return None


def _first_original_duration_ms(transcripts: list[dict[str, Any]]) -> int | None:
    for transcript in transcripts:
        value = transcript.get("original_duration_in_milliseconds")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _task_id_from_submit(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    output = payload.get("output")
    if isinstance(output, dict):
        task_id = output.get("task_id")
        if isinstance(task_id, str) and task_id:
            return task_id
    task_id = payload.get("task_id")
    if isinstance(task_id, str) and task_id:
        return task_id
    return None


def _json_or_text(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return response.text


if __name__ == "__main__":
    raise SystemExit(main())
