from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import math
import time
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from loguru import logger

from app.services.document_parser.formats.media.extract import SOURCE_AUDIO_REL
from app.services.document_parser.support.parser_log_utils import truncate_log_value

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import LLMServiceException
from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.services.http.client_pool import get_sync_client
from shared.services.storage.job_file_storage import JobFileStorage

# DashScope SDK `jobs.wait` defaults (not product thresholds).
_POLL_INTERVAL_SECONDS = 1.0
_MAX_POLL_INTERVAL_SECONDS = 8.0
_DEFAULT_SPEECH_MODEL = "fun-asr"
_TERMINAL_FAILURE_STATUSES = frozenset({"FAILED", "CANCELED", "UNKNOWN"})


@dataclass(frozen=True)
class MediaAudio:
    path: str
    job_id: str | None = None


@dataclass(frozen=True)
class TranscriptSentence:
    start_ms: int
    end_ms: int
    text: str
    speaker_id: str | None


@dataclass(frozen=True)
class Transcript:
    sentences: tuple[TranscriptSentence, ...]


class SpeechTranscriber(Protocol):
    def transcribe(
        self,
        audio: MediaAudio,
        *,
        diarize: bool,
        language_hint: str | None,
    ) -> Transcript: ...


class FunAsrSpeechTranscriber:
    """DashScope Fun-ASR file transcription (async job, public file URL)."""

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._client = client

    def transcribe(
        self,
        audio: MediaAudio,
        *,
        diarize: bool,
        language_hint: str | None,
    ) -> Transcript:
        from shared.services.ai.ali_quota_manager import get_ali_quota_manager

        model = (settings.SPEECH_MODEL or "").strip() or _DEFAULT_SPEECH_MODEL
        api_root = _dashscope_api_v1_root(settings.ALI_URL)
        parameters: dict[str, Any] = {"diarization_enabled": bool(diarize)}
        if language_hint:
            parameters["language_hints"] = [language_hint]

        quota_manager = get_ali_quota_manager()
        client = self._client or get_sync_client()
        max_retries = settings.ALI_INLINE_MAX_RETRIES
        last_error: Exception | None = None
        with _staged_source_audio_url(audio) as file_url:
            for attempt in range(max_retries):
                lease = quota_manager.acquire_request(operation="fun_asr_transcribe")
                try:
                    task_id = _submit_transcription(
                        client,
                        api_root=api_root,
                        api_key=lease.api_key,
                        model=model,
                        file_url=file_url,
                        parameters=parameters,
                    )
                    job = _wait_for_task(
                        client,
                        api_root=api_root,
                        api_key=lease.api_key,
                        task_id=task_id,
                    )
                    transcripts = _download_transcripts(client, job)
                    return Transcript(sentences=_sentences_from_transcripts(transcripts))
                except KnowhereException:
                    raise
                except _FunAsrRateLimited as exc:
                    last_error = exc
                    quota_manager.mark_rate_limited(lease.token_id, exc.retry_after)
                    logger.warning(
                        "Fun-ASR token {} rate-limited (attempt {}/{}), "
                        "cooling down {}s and retrying with next token",
                        lease.token_id,
                        attempt + 1,
                        max_retries,
                        exc.retry_after,
                    )
                    if attempt == max_retries - 1:
                        raise LLMServiceException(
                            internal_message=(
                                f"All Ali tokens exhausted after {max_retries} "
                                f"Fun-ASR retries: {exc}"
                            ),
                            provider=model,
                            original_exception=exc,
                        ) from exc
                except Exception as exc:
                    raise LLMServiceException(
                        internal_message=f"Fun-ASR transcription failed: {exc}",
                        provider=model,
                        user_message="Speech transcription failed for this media file.",
                        original_exception=exc,
                    ) from exc
        raise LLMServiceException(
            internal_message="Fun-ASR retry loop exited without a transcript",
            provider=model,
            original_exception=last_error,
        )


class _FunAsrRateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Fun-ASR rate limited; retry_after={retry_after}")
        self.retry_after = retry_after


@contextmanager
def _staged_source_audio_url(audio: MediaAudio) -> Iterator[str]:
    job_id = (audio.job_id or "").strip()
    if not job_id:
        raise LLMServiceException(
            internal_message=(
                "Fun-ASR needs a job_id to publish source_audio.wav for a "
                f"presigned GET: path={audio.path}"
            ),
            provider=(settings.SPEECH_MODEL or "").strip() or _DEFAULT_SPEECH_MODEL,
            user_message="Speech transcription failed for this media file.",
        )
    storage = JobFileStorage()
    storage_key = (
        f"{storage.build_result_raw_prefix(job_id=job_id)}"
        f"asr/{uuid4().hex}/{SOURCE_AUDIO_REL}"
    )
    storage.upload_local_file(
        audio.path,
        storage_key,
        bucket=storage.results_bucket,
    )
    try:
        signed = storage.generate_download_url(
            storage_key,
            bucket=storage.results_bucket,
        )
        download_url = signed.get("download_url")
        if not isinstance(download_url, str) or not download_url:
            raise LLMServiceException(
                internal_message=(
                    "Fun-ASR source_audio presign returned no download_url: "
                    f"job_id={job_id}, storage_key={storage_key}"
                ),
                provider=(settings.SPEECH_MODEL or "").strip() or _DEFAULT_SPEECH_MODEL,
                user_message="Speech transcription failed for this media file.",
            )
        yield download_url
    finally:
        try:
            storage.delete_object(storage_key, bucket=storage.results_bucket)
        except Exception as exc:
            logger.warning(
                "Failed to delete staged Fun-ASR audio object: "
                "storage_key={}, error={}",
                storage_key,
                exc,
            )


def _dashscope_api_v1_root(ali_url: str) -> str:
    parsed = urlparse(ali_url)
    if not parsed.scheme or not parsed.netloc:
        raise LLMServiceException(
            internal_message=f"ALI_URL is not a valid DashScope URL: {ali_url}",
            provider=(settings.SPEECH_MODEL or "").strip() or _DEFAULT_SPEECH_MODEL,
            user_message="Speech transcription is not configured.",
        )
    return f"{parsed.scheme}://{parsed.netloc}/api/v1"


def _submit_transcription(
    client: httpx.Client,
    *,
    api_root: str,
    api_key: str,
    model: str,
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
            "model": model,
            "input": {"file_urls": [file_url]},
            "parameters": parameters,
        },
    )
    _raise_for_fun_asr_http(response, action="submit")
    payload = _json_or_text(response)
    task_id = _task_id_from_submit(payload)
    if not task_id:
        raise RuntimeError(f"Fun-ASR submit returned no task_id: {payload}")
    logger.info(f"Submitted Fun-ASR task {task_id}")
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
        _raise_for_fun_asr_http(response, action="poll")
        payload = _json_or_text(response)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Fun-ASR poll returned a non-object: {payload}")
        status = str(
            payload.get("output", {}).get("task_status")
            or payload.get("task_status")
            or ""
        ).upper()
        logger.info(f"Fun-ASR task {task_id} status={status}")
        if status == "SUCCEEDED":
            return payload
        if not status:
            raise RuntimeError(f"Fun-ASR poll returned no task_status: {payload}")
        if status in _TERMINAL_FAILURE_STATUSES:
            raise RuntimeError(f"Fun-ASR task {status.lower()}: {payload}")
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
    if not isinstance(results, list) or not results:
        raise RuntimeError(f"Fun-ASR job returned no subtask results: {job}")
    transcripts: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            raise RuntimeError(f"Fun-ASR subtask result is not an object: {result}")
        subtask_status = str(result.get("subtask_status") or "").upper()
        if subtask_status and subtask_status != "SUCCEEDED":
            raise RuntimeError(f"Fun-ASR subtask failed: {result}")
        transcription_url = result.get("transcription_url")
        if not isinstance(transcription_url, str) or not transcription_url:
            raise RuntimeError(
                f"Fun-ASR subtask succeeded without transcription_url: {result}"
            )
        response = client.get(transcription_url)
        _raise_for_fun_asr_http(response, action="download")
        payload = _json_or_text(response)
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Fun-ASR transcript payload is not a JSON object: {payload}"
            )
        transcripts.append(payload)
    return transcripts


def _sentences_from_transcripts(
    transcripts: list[dict[str, Any]],
) -> tuple[TranscriptSentence, ...]:
    sentences: list[TranscriptSentence] = []
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
                parsed = _transcript_sentence_from_payload(sentence)
                if parsed is not None:
                    sentences.append(parsed)
    sentences.sort(key=lambda item: (item.start_ms, item.end_ms))
    return tuple(sentences)


def _transcript_sentence_from_payload(sentence: object) -> TranscriptSentence | None:
    if not isinstance(sentence, dict):
        raise LLMServiceException(
            internal_message=f"Fun-ASR sentence is not an object: {sentence}",
            provider=(settings.SPEECH_MODEL or "").strip() or _DEFAULT_SPEECH_MODEL,
            user_message="Speech transcription failed for this media file.",
        )
    text = str(sentence.get("text") or "").strip()
    if not text:
        return None
    start_ms = _require_timestamp_ms(sentence.get("begin_time"), field="begin_time")
    end_ms = _require_timestamp_ms(sentence.get("end_time"), field="end_time")
    if end_ms < start_ms:
        raise LLMServiceException(
            internal_message=(
                f"Fun-ASR sentence end_time precedes begin_time: {sentence}"
            ),
            provider=(settings.SPEECH_MODEL or "").strip() or _DEFAULT_SPEECH_MODEL,
            user_message="Speech transcription failed for this media file.",
        )
    return TranscriptSentence(
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        speaker_id=_speaker_id_from_payload(sentence.get("speaker_id")),
    )


def _require_timestamp_ms(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LLMServiceException(
            internal_message=f"Fun-ASR sentence {field} is not a number: {value!r}",
            provider=(settings.SPEECH_MODEL or "").strip() or _DEFAULT_SPEECH_MODEL,
            user_message="Speech transcription failed for this media file.",
        )
    if not math.isfinite(value) or value < 0:
        raise LLMServiceException(
            internal_message=f"Fun-ASR sentence {field} is not a valid timestamp: {value!r}",
            provider=(settings.SPEECH_MODEL or "").strip() or _DEFAULT_SPEECH_MODEL,
            user_message="Speech transcription failed for this media file.",
        )
    return int(value)


def _speaker_id_from_payload(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return str(value)


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


def _raise_for_fun_asr_http(response: httpx.Response, *, action: str) -> None:
    if response.status_code == 429:
        retry_after = settings.ALI_TOKEN_COOLDOWN_SECONDS
        header = response.headers.get("Retry-After")
        if header:
            try:
                retry_after = max(int(float(header)), 1)
            except ValueError:
                retry_after = settings.ALI_TOKEN_COOLDOWN_SECONDS
        raise _FunAsrRateLimited(retry_after)
    if response.status_code < 400:
        return
    payload = _json_or_text(response)
    raise RuntimeError(
        f"Fun-ASR {action} failed: status={response.status_code} "
        f"body={truncate_log_value(payload)}"
    )


def _json_or_text(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return response.text
