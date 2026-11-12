from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from app.services.document_parser.formats.media.extract import SOURCE_AUDIO_REL
from app.services.document_parser.formats.media.parser import (
    MEDIA_TRANSCRIPT_REL,
    parse_media,
    parse_media_transcript,
)
from app.services.document_parser.formats.media.probe import media_info_from_ffprobe_payload
from app.services.document_parser.formats.media.transcriber import (
    MediaAudio,
    Transcript,
    TranscriptSentence,
    _sentences_from_transcripts,
)
from shared.core.exceptions.domain_exceptions import ValidationException
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
from shared.services.storage.zip_chunk_schema import ZipChunkSchemaBuilder

_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class _FakeSpeechTranscriber:
    def __init__(self, sentences: list[TranscriptSentence]) -> None:
        self._transcript = Transcript(sentences=tuple(sentences))
        self.calls: list[dict[str, object]] = []

    def transcribe(
        self,
        audio: MediaAudio,
        *,
        diarize: bool,
        language_hint: str | None,
    ) -> Transcript:
        self.calls.append(
            {
                "audio_path": audio.path,
                "job_id": audio.job_id,
                "diarize": diarize,
                "language_hint": language_hint,
            }
        )
        return self._transcript


class _MemoryAsrTaskStore:
    def __init__(self, ref: object | None = None) -> None:
        self.ref = ref
        self.saved: list[tuple[str, object]] = []

    def load(self, job_id: str) -> object | None:
        return self.ref

    def save(self, job_id: str, ref: object) -> None:
        self.saved.append((job_id, ref))
        self.ref = ref


def _audio_info() -> object:
    return media_info_from_ffprobe_payload(
        {
            "format": {"duration": "12.5"},
            "streams": [
                {"codec_type": "audio", "codec_name": "mp3", "channels": 2},
            ],
        }
    )


def test_parse_media_with_fake_transcriber_emits_timestamped_text_chunks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services.document_parser.formats.media import parser as parser_module

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    audio_path = output_dir / SOURCE_AUDIO_REL
    audio_path.write_bytes(b"RIFF")
    monkeypatch.setattr(parser_module, "admit_media_source", lambda _path: _audio_info())
    monkeypatch.setattr(
        parser_module,
        "extract_source_audio",
        lambda _path, _output_dir: str(audio_path),
    )
    transcriber = _FakeSpeechTranscriber(
        [
            TranscriptSentence(start_ms=8410, end_ms=10290, text="Hi, I'm Karen.", speaker_id="0"),
            TranscriptSentence(start_ms=10290, end_ms=11290, text="and I'm Bradley.", speaker_id="1"),
        ]
    )

    parsed = parse_media(
        str(tmp_path / "talk.mp3"),
        filename="talk.mp3",
        output_dir=str(output_dir),
        relative_root="talk.mp3",
        chapterize=False,
        job_id="job_talk",
        transcriber=transcriber,
    )

    assert transcriber.calls == [
        {
            "audio_path": str(audio_path),
            "job_id": "job_talk",
            "diarize": True,
            "language_hint": None,
        }
    ]
    assert parsed.rows_count == 2
    assert parsed.parsed_df is not None
    first = parsed.parsed_df.iloc[0]
    second = parsed.parsed_df.iloc[1]
    assert first["type"] == "PTXT"
    assert first["content"] == "S1: Hi, I'm Karen."
    assert second["content"] == "S2: and I'm Bradley."
    assert first["path"] == "talk.mp3"
    assert second["path"] == "talk.mp3"
    transcript = json.loads((output_dir / MEDIA_TRANSCRIPT_REL).read_text(encoding="utf-8"))
    assert transcript == {
        "media": {"kind": "audio", "duration_ms": 12500},
        "sentences": [
            {"start_ms": 8410, "end_ms": 10290, "text": "Hi, I'm Karen.", "speaker_id": "0"},
            {"start_ms": 10290, "end_ms": 11290, "text": "and I'm Bradley.", "speaker_id": "1"},
        ],
    }
    first_meta = json.loads(str(first["extra_metadata"]))
    assert first_meta == {
        "time_ranges": [{"start_ms": 8410, "end_ms": 10290}],
        "speakers": ["S1"],
        "media": {"kind": "audio", "duration_ms": 12500},
        "source_kind": "asr",
    }
    chunks = dataframe_to_chunks(parsed.parsed_df)
    assert chunks[0]["type"] == "text"
    assert chunks[0]["metadata"]["time_ranges"] == [{"start_ms": 8410, "end_ms": 10290}]
    assert chunks[0]["metadata"]["speakers"] == ["S1"]
    assert chunks[0]["metadata"]["source_kind"] == "asr"
    assert chunks[1]["metadata"]["speakers"] == ["S2"]
    formatted = ZipChunkSchemaBuilder().format_chunks(
        chunks,
        image_files_map={},
        table_files_map={},
    )
    zip_meta = formatted[0]["metadata"]
    assert zip_meta["time_ranges"] == [{"start_ms": 8410, "end_ms": 10290}]
    assert zip_meta["speakers"] == ["S1"]
    assert zip_meta["media"] == {"kind": "audio", "duration_ms": 12500}
    assert zip_meta["source_kind"] == "asr"


def test_parse_media_video_filename_sets_media_kind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services.document_parser.formats.media import parser as parser_module

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    audio_path = output_dir / SOURCE_AUDIO_REL
    audio_path.write_bytes(b"RIFF")
    monkeypatch.setattr(parser_module, "admit_media_source", lambda _path: _audio_info())
    monkeypatch.setattr(
        parser_module,
        "extract_source_audio",
        lambda _path, _output_dir: str(audio_path),
    )
    parsed = parse_media(
        str(tmp_path / "lecture.mp4"),
        filename="lecture.mp4",
        output_dir=str(output_dir),
        relative_root="lecture.mp4",
        chapterize=False,
        transcriber=_FakeSpeechTranscriber(
            [TranscriptSentence(start_ms=0, end_ms=1000, text="welcome", speaker_id="0")]
        ),
    )
    assert parsed.parsed_df is not None
    metadata = json.loads(str(parsed.parsed_df.iloc[0]["extra_metadata"]))
    assert metadata["media"]["kind"] == "video"
    assert parsed.parsed_df.iloc[0]["path"] == "lecture.mp4"


def test_parse_media_fails_closed_on_empty_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services.document_parser.formats.media import parser as parser_module

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    audio_path = output_dir / SOURCE_AUDIO_REL
    audio_path.write_bytes(b"RIFF")
    monkeypatch.setattr(parser_module, "admit_media_source", lambda _path: _audio_info())
    monkeypatch.setattr(
        parser_module,
        "extract_source_audio",
        lambda _path, _output_dir: str(audio_path),
    )
    with pytest.raises(ValidationException) as exc_info:
        parse_media(
            str(tmp_path / "silent.mp3"),
            filename="silent.mp3",
            output_dir=str(output_dir),
            relative_root="silent.mp3",
            chapterize=False,
            transcriber=_FakeSpeechTranscriber([]),
        )
    assert "no recognizable speech" in exc_info.value.user_message


def test_fun_asr_sentence_mapping_keeps_timestamps_and_speaker_ids() -> None:
    sentences = _sentences_from_transcripts(
        [
            {
                "transcripts": [
                    {
                        "sentences": [
                            {
                                "begin_time": 8410,
                                "end_time": 10290,
                                "text": "Hi, I'm Karen Sandler. ",
                                "speaker_id": 0,
                            },
                            {
                                "begin_time": 10290,
                                "end_time": 11290,
                                "text": " and I'm Bradley Coon.",
                                "speaker_id": 1,
                            },
                            {
                                "begin_time": 11970,
                                "end_time": 14170,
                                "text": "   ",
                                "speaker_id": 0,
                            },
                        ]
                    }
                ]
            }
        ]
    )
    assert sentences == (
        TranscriptSentence(
            start_ms=8410,
            end_ms=10290,
            text="Hi, I'm Karen Sandler.",
            speaker_id="0",
        ),
        TranscriptSentence(
            start_ms=10290,
            end_ms=11290,
            text="and I'm Bradley Coon.",
            speaker_id="1",
        ),
    )


@pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg and ffprobe must be on PATH")
def test_extract_source_audio_downmixes_stereo_to_16khz_mono(tmp_path: Path) -> None:
    from app.services.document_parser.formats.media.extract import (
        ASR_SAMPLE_RATE_HZ,
        extract_source_audio,
    )
    from app.services.document_parser.formats.media.probe import probe_media

    stereo = tmp_path / "stereo.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-ac",
            "2",
            str(stereo),
        ],
        check=True,
    )
    output_dir = tmp_path / "out"
    extracted = Path(extract_source_audio(str(stereo), str(output_dir)))
    assert extracted.name == SOURCE_AUDIO_REL
    info = probe_media(str(extracted))
    assert info.has_audio is True
    assert info.channels == 1
    assert info.codec == "pcm_s16le"
    probe_json = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                str(extracted),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert int(probe_json["streams"][0]["sample_rate"]) == ASR_SAMPLE_RATE_HZ


def test_parse_media_disambiguates_identical_utterances_at_different_times(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services.document_parser.formats.media import parser as parser_module

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    audio_path = output_dir / SOURCE_AUDIO_REL
    audio_path.write_bytes(b"RIFF")
    monkeypatch.setattr(parser_module, "admit_media_source", lambda _path: _audio_info())
    monkeypatch.setattr(
        parser_module,
        "extract_source_audio",
        lambda _path, _output_dir: str(audio_path),
    )
    sentences = [
        TranscriptSentence(start_ms=1000, end_ms=1500, text="Yes.", speaker_id="0"),
        TranscriptSentence(start_ms=2000, end_ms=2500, text="Go on.", speaker_id="1"),
        TranscriptSentence(start_ms=5000, end_ms=5500, text="Yes.", speaker_id="0"),
    ]
    first_parse = parse_media(
        str(tmp_path / "talk.mp3"),
        filename="talk.mp3",
        output_dir=str(output_dir),
        relative_root="talk.mp3",
        chapterize=False,
        transcriber=_FakeSpeechTranscriber(sentences),
    )
    second_parse = parse_media(
        str(tmp_path / "talk.mp3"),
        filename="talk.mp3",
        output_dir=str(output_dir),
        relative_root="talk.mp3",
        chapterize=False,
        transcriber=_FakeSpeechTranscriber(sentences),
    )

    first_chunks = dataframe_to_chunks(first_parse.parsed_df)
    second_chunks = dataframe_to_chunks(second_parse.parsed_df)
    assert first_chunks[0]["content"] == first_chunks[2]["content"] == "S1: Yes."
    assert first_chunks[0]["chunk_id"] != first_chunks[2]["chunk_id"]
    assert [c["chunk_id"] for c in first_chunks] == [
        c["chunk_id"] for c in second_chunks
    ]


@pytest.mark.parametrize(
    "poll_payload",
    [
        {"output": {"task_status": "CANCELED"}},
        {"output": {"task_status": "UNKNOWN"}},
        {"output": {}},
    ],
)
def test_wait_for_task_fails_immediately_on_terminal_or_missing_status(
    monkeypatch: pytest.MonkeyPatch,
    poll_payload: dict[str, object],
) -> None:
    import httpx
    from app.services.document_parser.formats.media import transcriber as transcriber_module

    monkeypatch.setattr(
        transcriber_module.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(AssertionError("must not sleep on terminal status")),
    )
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=poll_payload)
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(RuntimeError):
            transcriber_module._wait_for_task(
                client,
                api_root="https://dashscope.aliyuncs.com/api/v1",
                api_key="test-key",
                task_id="task_123",
            )


@pytest.mark.parametrize("fail_task", [False, True])
def test_fun_asr_transcriber_deletes_staged_audio_object_after_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fail_task: bool,
) -> None:
    from types import SimpleNamespace
    import httpx
    from app.services.document_parser.formats.media import transcriber as transcriber_module
    from shared.core.exceptions.domain_exceptions import LLMServiceException

    uploaded: list[tuple[str, str, str]] = []
    deleted: list[tuple[str, str]] = []

    class _FakeStorage:
        results_bucket = "results-bucket"

        def build_result_raw_prefix(self, *, job_id: str) -> str:
            return f"results/{job_id}/"

        def upload_local_file(
            self, local_path: str, storage_key: str, *, bucket: str
        ) -> dict[str, object]:
            uploaded.append((local_path, storage_key, bucket))
            return {}

        def generate_download_url(
            self, storage_key: str, *, bucket: str
        ) -> dict[str, str]:
            return {"download_url": f"https://storage.example/{bucket}/{storage_key}"}

        def delete_object(self, storage_key: str, *, bucket: str) -> bool:
            deleted.append((storage_key, bucket))
            return True

    class _FakeQuotaManager:
        def acquire_request(self, *, operation: str) -> object:
            assert operation == "fun_asr_transcribe"
            return SimpleNamespace(token_id="tok1", api_key="key1")

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"output": {"task_id": "task_1"}})
        if request.url.path.endswith("/tasks/task_1"):
            if fail_task:
                return httpx.Response(200, json={"output": {"task_status": "CANCELED"}})
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                "transcription_url": "https://dashscope.aliyuncs.com/transcript.json",
                            }
                        ],
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "transcripts": [
                    {
                        "sentences": [
                            {
                                "begin_time": 0,
                                "end_time": 1000,
                                "text": "hello",
                                "speaker_id": 0,
                            }
                        ]
                    }
                ]
            },
        )

    monkeypatch.setattr(transcriber_module, "JobFileStorage", _FakeStorage)
    monkeypatch.setattr(
        "shared.services.ai.ali_quota_manager.get_ali_quota_manager",
        lambda: _FakeQuotaManager(),
    )
    audio_file = tmp_path / SOURCE_AUDIO_REL
    audio_file.write_bytes(b"RIFF")

    with httpx.Client(transport=httpx.MockTransport(_handler)) as client:
        transcriber = transcriber_module.FunAsrSpeechTranscriber(
            client=client, task_store=_MemoryAsrTaskStore()
        )
        if fail_task:
            with pytest.raises(LLMServiceException):
                transcriber.transcribe(
                    MediaAudio(path=str(audio_file), job_id="job_1"),
                    diarize=True,
                    language_hint=None,
                )
        else:
            result = transcriber.transcribe(
                MediaAudio(path=str(audio_file), job_id="job_1"),
                diarize=True,
                language_hint=None,
            )
            assert len(result.sentences) == 1

    assert len(uploaded) == 1
    _, staged_key, staged_bucket = uploaded[0]
    assert staged_key.startswith("results/job_1/asr/")
    assert staged_key.endswith(f"/{SOURCE_AUDIO_REL}")
    assert deleted == [(staged_key, staged_bucket)]


@pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg and ffprobe must be on PATH")
def test_parse_media_generated_tone_with_fake_transcriber(tmp_path: Path) -> None:
    tone = tmp_path / "tone.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            str(tone),
        ],
        check=True,
    )
    output_dir = tmp_path / "out"
    parsed = parse_media(
        str(tone),
        filename="tone.wav",
        output_dir=str(output_dir),
        relative_root="tone.wav",
        chapterize=False,
        transcriber=_FakeSpeechTranscriber(
            [TranscriptSentence(start_ms=0, end_ms=1000, text="tone", speaker_id="0")]
        ),
    )
    assert (output_dir / SOURCE_AUDIO_REL).is_file()
    assert parsed.rows_count == 1
    assert parsed.parsed_df is not None
    assert parsed.parsed_df.iloc[0]["content"] == "S1: tone"


@pytest.mark.parametrize("rate_limited_action", ["submit", "poll"])
def test_fun_asr_rate_limit_only_resubmits_before_a_task_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rate_limited_action: str,
) -> None:
    from types import SimpleNamespace
    import httpx
    from app.services.document_parser.formats.media import transcriber as transcriber_module

    class _FakeStorage:
        results_bucket = "results-bucket"

        def build_result_raw_prefix(self, *, job_id: str) -> str:
            return f"results/{job_id}/"

        def upload_local_file(
            self, local_path: str, storage_key: str, *, bucket: str
        ) -> dict[str, object]:
            return {}

        def generate_download_url(
            self, storage_key: str, *, bucket: str
        ) -> dict[str, str]:
            return {"download_url": f"https://storage.example/{bucket}/{storage_key}"}

        def delete_object(self, storage_key: str, *, bucket: str) -> bool:
            return True

    leases = iter(
        [
            SimpleNamespace(token_id="tok1", api_key="key1"),
            SimpleNamespace(token_id="tok2", api_key="key2"),
        ]
    )
    rate_limited_tokens: list[tuple[str, int]] = []

    class _FakeQuotaManager:
        def acquire_request(self, *, operation: str) -> object:
            return next(leases)

        def mark_rate_limited(self, token_id: str, retry_after: int) -> None:
            rate_limited_tokens.append((token_id, retry_after))

    submit_keys: list[str] = []
    poll_keys: list[str] = []
    sleeps: list[float] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        api_key = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if request.method == "POST":
            submit_keys.append(api_key)
            if rate_limited_action == "submit" and len(submit_keys) == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
            return httpx.Response(200, json={"output": {"task_id": "task_1"}})
        if request.url.path.endswith("/tasks/task_1"):
            poll_keys.append(api_key)
            if rate_limited_action == "poll" and len(poll_keys) == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                "transcription_url": "https://dashscope.aliyuncs.com/transcript.json",
                            }
                        ],
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "transcripts": [
                    {
                        "sentences": [
                            {"begin_time": 0, "end_time": 1000, "text": "hello", "speaker_id": 0}
                        ]
                    }
                ]
            },
        )

    monkeypatch.setattr(transcriber_module, "JobFileStorage", _FakeStorage)
    monkeypatch.setattr(transcriber_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        "shared.services.ai.ali_quota_manager.get_ali_quota_manager",
        lambda: _FakeQuotaManager(),
    )
    audio_file = tmp_path / SOURCE_AUDIO_REL
    audio_file.write_bytes(b"RIFF")

    with httpx.Client(transport=httpx.MockTransport(_handler)) as client:
        result = transcriber_module.FunAsrSpeechTranscriber(
            client=client, task_store=_MemoryAsrTaskStore()
        ).transcribe(
            MediaAudio(path=str(audio_file), job_id="job_1"),
            diarize=True,
            language_hint=None,
        )

    assert len(result.sentences) == 1
    if rate_limited_action == "submit":
        assert submit_keys == ["key1", "key2"]
        assert rate_limited_tokens == [("tok1", 7)]
        assert poll_keys == ["key2"]
        assert sleeps == []
    else:
        assert submit_keys == ["key1"]
        assert rate_limited_tokens == []
        assert poll_keys == ["key1", "key1"]
        assert sleeps == [7]


def _succeeded_task_handler(
    calls: list[str],
    *,
    resumed_status: str = "SUCCEEDED",
) -> object:
    import httpx

    def _handler(request: httpx.Request) -> httpx.Response:
        api_key = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if request.method == "POST":
            calls.append(f"submit:{api_key}")
            return httpx.Response(200, json={"output": {"task_id": "task_new"}})
        if "/tasks/" in request.url.path:
            task_id = request.url.path.rsplit("/", 1)[-1]
            calls.append(f"poll:{task_id}:{api_key}")
            status = resumed_status if task_id == "task_old" else "SUCCEEDED"
            return httpx.Response(
                200,
                json={
                    "usage": {"duration": 42},
                    "output": {
                        "task_status": status,
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                "transcription_url": "https://dashscope.aliyuncs.com/transcript.json",
                            }
                        ],
                    }
                },
            )
        calls.append("download")
        return httpx.Response(
            200,
            json={
                "transcripts": [
                    {
                        "sentences": [
                            {"begin_time": 0, "end_time": 1000, "text": "hello", "speaker_id": 0}
                        ]
                    }
                ]
            },
        )

    return _handler


def _patch_fun_asr_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    staged: list[str],
) -> None:
    from types import SimpleNamespace
    from app.services.document_parser.formats.media import transcriber as transcriber_module

    class _FakeStorage:
        results_bucket = "results-bucket"

        def build_result_raw_prefix(self, *, job_id: str) -> str:
            return f"results/{job_id}/"

        def upload_local_file(
            self, local_path: str, storage_key: str, *, bucket: str
        ) -> dict[str, object]:
            staged.append(storage_key)
            return {}

        def generate_download_url(
            self, storage_key: str, *, bucket: str
        ) -> dict[str, str]:
            return {"download_url": f"https://storage.example/{bucket}/{storage_key}"}

        def delete_object(self, storage_key: str, *, bucket: str) -> bool:
            return True

    class _FakeQuotaManager:
        tokens = [
            SimpleNamespace(token_id="tok1", api_key="key1"),
            SimpleNamespace(token_id="tok2", api_key="key2"),
        ]

        def acquire_request(self, *, operation: str) -> object:
            return self.tokens[1]

        def mark_rate_limited(self, token_id: str, retry_after: int) -> None:
            raise AssertionError("no 429 expected")

    monkeypatch.setattr(transcriber_module, "JobFileStorage", _FakeStorage)
    monkeypatch.setattr(transcriber_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "shared.services.ai.ali_quota_manager.get_ali_quota_manager",
        lambda: _FakeQuotaManager(),
    )


def test_fun_asr_records_submitted_task_for_redelivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import httpx
    from app.services.document_parser.formats.media import transcriber as transcriber_module
    from app.services.document_parser.formats.media.asr_task_store import AsrTaskRef

    calls: list[str] = []
    staged: list[str] = []
    _patch_fun_asr_dependencies(monkeypatch, staged)
    store = _MemoryAsrTaskStore()
    audio_file = tmp_path / SOURCE_AUDIO_REL
    audio_file.write_bytes(b"RIFF")

    with httpx.Client(transport=httpx.MockTransport(_succeeded_task_handler(calls))) as client:
        result = transcriber_module.FunAsrSpeechTranscriber(
            client=client, task_store=store
        ).transcribe(
            MediaAudio(path=str(audio_file), job_id="job_1"),
            diarize=True,
            language_hint=None,
        )

    assert len(result.sentences) == 1
    assert store.saved == [("job_1", AsrTaskRef(task_id="task_new", token_id="tok2"))]
    assert calls == ["submit:key2", "poll:task_new:key2", "download"]
    assert len(staged) == 1


def test_fun_asr_redelivery_resumes_submitted_task_without_resubmitting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import httpx
    from app.services.document_parser.formats.media import transcriber as transcriber_module
    from app.services.document_parser.formats.media.asr_task_store import AsrTaskRef

    calls: list[str] = []
    staged: list[str] = []
    _patch_fun_asr_dependencies(monkeypatch, staged)
    store = _MemoryAsrTaskStore(AsrTaskRef(task_id="task_old", token_id="tok1"))
    audio_file = tmp_path / SOURCE_AUDIO_REL
    audio_file.write_bytes(b"RIFF")

    with httpx.Client(transport=httpx.MockTransport(_succeeded_task_handler(calls))) as client:
        result = transcriber_module.FunAsrSpeechTranscriber(
            client=client, task_store=store
        ).transcribe(
            MediaAudio(path=str(audio_file), job_id="job_1"),
            diarize=True,
            language_hint=None,
        )

    assert len(result.sentences) == 1
    assert calls == ["poll:task_old:key1", "download"]
    assert staged == []
    assert store.saved == []


def test_fun_asr_resubmits_when_the_resumed_task_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import httpx
    from app.services.document_parser.formats.media import transcriber as transcriber_module
    from app.services.document_parser.formats.media.asr_task_store import AsrTaskRef

    calls: list[str] = []
    staged: list[str] = []
    _patch_fun_asr_dependencies(monkeypatch, staged)
    store = _MemoryAsrTaskStore(AsrTaskRef(task_id="task_old", token_id="tok1"))
    audio_file = tmp_path / SOURCE_AUDIO_REL
    audio_file.write_bytes(b"RIFF")

    handler = _succeeded_task_handler(calls, resumed_status="FAILED")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = transcriber_module.FunAsrSpeechTranscriber(
            client=client, task_store=store
        ).transcribe(
            MediaAudio(path=str(audio_file), job_id="job_1"),
            diarize=True,
            language_hint=None,
        )

    assert len(result.sentences) == 1
    assert calls == [
        "poll:task_old:key1",
        "submit:key2",
        "poll:task_new:key2",
        "download",
    ]
    assert store.saved == [("job_1", AsrTaskRef(task_id="task_new", token_id="tok2"))]


def test_fun_asr_soft_time_limit_is_not_reported_as_transcription_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import httpx
    from celery.exceptions import SoftTimeLimitExceeded
    from app.services.document_parser.formats.media import transcriber as transcriber_module

    def _running(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"output": {"task_id": "task_new"}})
        return httpx.Response(200, json={"output": {"task_status": "RUNNING"}})

    def _soft_limit(_seconds: float) -> None:
        raise SoftTimeLimitExceeded()

    _patch_fun_asr_dependencies(monkeypatch, [])
    monkeypatch.setattr(transcriber_module.time, "sleep", _soft_limit)
    audio_file = tmp_path / SOURCE_AUDIO_REL
    audio_file.write_bytes(b"RIFF")

    with httpx.Client(transport=httpx.MockTransport(_running)) as client:
        with pytest.raises(SoftTimeLimitExceeded):
            transcriber_module.FunAsrSpeechTranscriber(
                client=client, task_store=_MemoryAsrTaskStore()
            ).transcribe(
                MediaAudio(path=str(audio_file), job_id="job_1"),
                diarize=True,
                language_hint=None,
            )


@pytest.mark.parametrize("stored_task", [False, True])
def test_fun_asr_records_billed_audio_seconds_once_per_finished_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stored_task: bool,
) -> None:
    import httpx
    from app.services.document_parser.formats.media import transcriber as transcriber_module
    from app.services.document_parser.formats.media.asr_task_store import AsrTaskRef
    from shared.core.config import settings
    from shared.services.ai.token_tracking import (
        cleanup_token_tracker,
        init_token_tracker,
    )

    calls: list[str] = []
    _patch_fun_asr_dependencies(monkeypatch, [])
    store = _MemoryAsrTaskStore(
        AsrTaskRef(task_id="task_old", token_id="tok1") if stored_task else None
    )
    audio_file = tmp_path / SOURCE_AUDIO_REL
    audio_file.write_bytes(b"RIFF")

    tracker = init_token_tracker()
    try:
        with httpx.Client(
            transport=httpx.MockTransport(_succeeded_task_handler(calls))
        ) as client:
            transcriber_module.FunAsrSpeechTranscriber(
                client=client, task_store=store
            ).transcribe(
                MediaAudio(path=str(audio_file), job_id="job_1"),
                diarize=True,
                language_hint=None,
            )
        usage = dict(tracker)
    finally:
        cleanup_token_tracker()

    asr_usage = usage["by_task"][transcriber_module.ASR_USAGE_TASK]
    assert asr_usage["audio_seconds"] == 42
    assert asr_usage["calls"] == 1
    assert asr_usage["total_tokens"] == 0
    assert usage["by_model"][settings.SPEECH_MODEL]["audio_seconds"] == 42


def test_billed_audio_seconds_requires_integer_usage_duration() -> None:
    from app.services.document_parser.formats.media.transcriber import (
        billed_audio_seconds,
    )

    assert billed_audio_seconds({"usage": {"duration": 3600}}) == 3600
    assert billed_audio_seconds({"usage": {}}) is None
    assert billed_audio_seconds({"usage": {"duration": True}}) is None
    assert billed_audio_seconds({}) is None


def _patch_outline(
    monkeypatch: pytest.MonkeyPatch,
    parser_module: object,
    chapter_titles: object,
) -> None:
    # Patch through the function globals: contract fixtures may reload modules.
    outline_globals = parser_module.build_media_outline.__globals__  # type: ignore[attr-defined]
    monkeypatch.setitem(outline_globals, "chapter_titles", chapter_titles)
    monkeypatch.setitem(outline_globals, "part_boundaries", lambda _digests: None)
    monkeypatch.setitem(outline_globals, "llm_summarize_scope", lambda **_kwargs: "")


def test_parse_media_chapters_segments_and_refine_rebuilds_them_from_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services.document_parser.formats.media import parser as parser_module

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    audio_path = output_dir / SOURCE_AUDIO_REL
    audio_path.write_bytes(b"RIFF")
    monkeypatch.setattr(parser_module, "admit_media_source", lambda _path: _audio_info())
    monkeypatch.setattr(
        parser_module,
        "extract_source_audio",
        lambda _path, _output_dir: str(audio_path),
    )
    chapter_calls: list[int] = []

    def _fake_chapter_titles(segments: list[object]) -> list[str]:
        chapter_calls.append(len(segments))
        return ["Intro", "Intro", "Pricing", "Intro"]

    _patch_outline(monkeypatch, parser_module, _fake_chapter_titles)
    transcriber = _FakeSpeechTranscriber(
        [
            TranscriptSentence(start_ms=0, end_ms=1000, text="welcome", speaker_id="0"),
            TranscriptSentence(start_ms=1000, end_ms=2000, text="thanks", speaker_id="1"),
            TranscriptSentence(start_ms=2000, end_ms=3000, text="price is", speaker_id="0"),
            TranscriptSentence(start_ms=3000, end_ms=4000, text="back to intro", speaker_id="1"),
        ]
    )

    parsed = parse_media(
        str(tmp_path / "talk.mp3"),
        filename="talk.mp3",
        output_dir=str(output_dir),
        relative_root="talk.mp3",
        chapterize=True,
        job_id="job_talk",
        transcriber=transcriber,
    )
    assert parsed.parsed_df is not None
    expected_paths = [
        "talk.mp3/Intro",
        "talk.mp3/Intro",
        "talk.mp3/Pricing",
        "talk.mp3/Intro_2",
    ]
    assert list(parsed.parsed_df["path"]) == expected_paths

    refined = parse_media_transcript(
        str(output_dir / MEDIA_TRANSCRIPT_REL),
        filename="talk.mp3",
        output_dir=str(output_dir),
        relative_root="talk.mp3",
    )
    assert refined.parsed_df is not None
    assert list(refined.parsed_df["path"]) == expected_paths
    assert list(refined.parsed_df["content"]) == list(parsed.parsed_df["content"])
    assert list(refined.parsed_df["extra_metadata"]) == list(
        parsed.parsed_df["extra_metadata"]
    )
    assert chapter_calls == [4, 4]
    assert len(transcriber.calls) == 1


def test_parse_media_keeps_flat_paths_when_chaptering_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services.document_parser.formats.media import parser as parser_module

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    audio_path = output_dir / SOURCE_AUDIO_REL
    audio_path.write_bytes(b"RIFF")
    monkeypatch.setattr(parser_module, "admit_media_source", lambda _path: _audio_info())
    monkeypatch.setattr(
        parser_module,
        "extract_source_audio",
        lambda _path, _output_dir: str(audio_path),
    )
    _patch_outline(monkeypatch, parser_module, lambda _segments: None)

    parsed = parse_media(
        str(tmp_path / "talk.mp3"),
        filename="talk.mp3",
        output_dir=str(output_dir),
        relative_root="talk.mp3",
        chapterize=True,
        job_id="job_talk",
        transcriber=_FakeSpeechTranscriber(
            [
                TranscriptSentence(start_ms=0, end_ms=1000, text="a", speaker_id="0"),
                TranscriptSentence(start_ms=1000, end_ms=2000, text="b", speaker_id="1"),
            ]
        ),
    )
    assert parsed.parsed_df is not None
    assert list(parsed.parsed_df["path"]) == ["talk.mp3", "talk.mp3"]
