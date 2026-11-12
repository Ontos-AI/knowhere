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
from app.services.document_parser.formats.media.parser import parse_media
from app.services.document_parser.formats.media.probe import media_info_from_ffprobe_payload
from app.services.document_parser.formats.media.transcriber import (
    MediaAudio,
    Transcript,
    TranscriptSentence,
    _sentences_from_transcripts,
)
from shared.core.exceptions.domain_exceptions import ValidationException
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks

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
    assert first["path"] == "talk.mp3/talk.mp3 part 1"
    assert second["path"] == "talk.mp3/talk.mp3 part 2"
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
def test_extract_source_audio_downmixes_stereo_to_mono(tmp_path: Path) -> None:
    from app.services.document_parser.formats.media.extract import extract_source_audio
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
            "sine=frequency=440:duration=1",
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
        transcriber=_FakeSpeechTranscriber(
            [TranscriptSentence(start_ms=0, end_ms=1000, text="tone", speaker_id="0")]
        ),
    )
    assert (output_dir / SOURCE_AUDIO_REL).is_file()
    assert parsed.rows_count == 1
    assert parsed.parsed_df is not None
    assert parsed.parsed_df.iloc[0]["content"] == "S1: tone"
