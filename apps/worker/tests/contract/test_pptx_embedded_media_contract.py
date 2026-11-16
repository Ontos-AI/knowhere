from __future__ import annotations

import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from lxml import etree

from app.services.document_parser.formats.media.probe import MediaInfo
from app.services.document_parser.formats.media.transcriber import (
    MediaAudio,
    Transcript,
    TranscriptSentence,
)
from app.services.document_parser.formats.pptx.embedded_media import (
    EmbeddedMediaSeparationError,
    separate_embedded_media,
)
from app.services.page_memory import presentation_media
from app.services.page_memory.presentation_media import (
    EMBEDDED_MEDIA_ASR_USAGE_TASK,
    EMBEDDED_MEDIA_FRAME_USAGE_TASK,
    EMBEDDED_MEDIA_SEPARATION_FAILED,
    PresentationMedia,
    attach_presentation_media,
    separate_presentation_media,
)

from shared.core.exceptions.domain_exceptions import (
    PresentationParsingException,
    ValidationException,
)
from shared.services.ai.summary import engine as summary_engine
from shared.services.ai.summary.model import AssetSummary
from shared.services.chunks.asset_descriptor import PageMedia

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"
NS = f'xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"'
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
VIDEO_REL = f"{R}/video"
AUDIO_REL = f"{R}/audio"
MEDIA_REL = "http://schemas.microsoft.com/office/2007/relationships/media"
IMAGE_REL = f"{R}/image"
SLIDE_W, SLIDE_H = 12192000, 6858000


def _rels(*relationships: tuple[str, str, str] | tuple[str, str, str, str]) -> str:
    items = []
    for relationship in relationships:
        rel_id, rel_type, target = relationship[:3]
        mode = ' TargetMode="External"' if len(relationship) == 4 else ""
        items.append(
            f'<Relationship Id="{rel_id}" Type="{rel_type}" Target="{target}"{mode}/>'
        )
    return f'<Relationships xmlns="{REL_NS}">{"".join(items)}</Relationships>'


def _media_picture(
    shape_id: int, name: str, file_tag: str, rel_attrs: str, p14_embed: str | None, xfrm: str
) -> str:
    p14 = (
        '<p:extLst><p:ext uri="{DAA4B4D4-6D71-4841-9C94-3DE7FCFB9230}">'
        f'<p14:media xmlns:p14="{P14}" r:embed="{p14_embed}"/></p:ext></p:extLst>'
        if p14_embed
        else ""
    )
    return (
        f'<p:pic><p:nvPicPr><p:cNvPr id="{shape_id}" name="{name}">'
        '<a:hlinkClick r:id="" action="ppaction://media"/></p:cNvPr>'
        f"<p:cNvPicPr/><p:nvPr><a:{file_tag} {rel_attrs}/>{p14}</p:nvPr></p:nvPicPr>"
        '<p:blipFill><a:blip r:embed="rIdPoster"/></p:blipFill>'
        f"<p:spPr>{xfrm}</p:spPr></p:pic>"
    )


def _xfrm(x: int, y: int, cx: int, cy: int) -> str:
    return f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'


def _slide(body: str, *, hidden: bool = False, timing: str = "", transition: str = "") -> str:
    show = ' show="0"' if hidden else ""
    return (
        f"<p:sld {NS}{show}><p:cSld><p:spTree>{body}</p:spTree></p:cSld>"
        f"{transition}{timing}</p:sld>"
    )


def _video_timing(shape_id: int) -> str:
    return (
        '<p:timing><p:tnLst><p:par><p:cTn id="1" nodeType="tmRoot"><p:childTnLst>'
        '<p:seq><p:cTn id="2"/></p:seq>'
        f'<p:video><p:cMediaNode><p:cTn id="3"/><p:tgtEl><p:spTgt spid="{shape_id}"/>'
        "</p:tgtEl></p:cMediaNode></p:video>"
        "</p:childTnLst></p:cTn></p:par></p:tnLst></p:timing>"
    )


def _build_package() -> bytes:
    """Four slides whose list order differs from their part names."""
    group = (
        "<p:grpSp><p:nvGrpSpPr/>"
        '<p:grpSpPr><a:xfrm><a:off x="1219200" y="685800"/><a:ext cx="6096000" cy="3429000"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="12192000" cy="6858000"/></a:xfrm></p:grpSpPr>'
        + _media_picture(
            9, "Narration", "audioFile", 'r:link="rId2"', None, _xfrm(0, 0, 6096000, 3429000)
        )
        + "</p:grpSp>"
    )
    slides = {
        # Listed first although its part is slide3.
        "ppt/slides/slide3.xml": _slide(
            _media_picture(
                6,
                "Demo",
                "videoFile",
                'r:link="rId2"',
                "rId1",
                _xfrm(0, 0, SLIDE_W // 2, SLIDE_H // 2),
            ),
            timing=_video_timing(6),
        ),
        "ppt/slides/slide1.xml": _slide(group, hidden=True),
        "ppt/slides/slide2.xml": _slide(
            _media_picture(4, "Linked", "videoFile", 'r:link="rId2"', None, _xfrm(0, 0, 1, 1))
        ),
        "ppt/slides/slide4.xml": _slide(
            "",
            transition='<p:transition><p:sndAc><p:stSnd><p:snd r:embed="rId3" name="click.wav"/>'
            "</p:stSnd></p:sndAc></p:transition>",
        ),
    }
    slide_rels = {
        "ppt/slides/_rels/slide3.xml.rels": _rels(
            ("rId1", MEDIA_REL, "../media/media1.mp4"),
            ("rId2", VIDEO_REL, "../media/media1.mp4"),
            ("rIdPoster", IMAGE_REL, "../media/image1.png"),
        ),
        "ppt/slides/_rels/slide1.xml.rels": _rels(
            ("rId2", AUDIO_REL, "../media/media2.m4a"),
            ("rIdPoster", IMAGE_REL, "../media/image1.png"),
        ),
        "ppt/slides/_rels/slide2.xml.rels": _rels(
            ("rId2", VIDEO_REL, "https://example.com/talk.mp4", "External"),
            ("rIdPoster", IMAGE_REL, "../media/image1.png"),
        ),
        "ppt/slides/_rels/slide4.xml.rels": _rels(
            ("rId3", AUDIO_REL, "../media/click.wav"),
        ),
    }
    presentation = (
        f'<p:presentation {NS}><p:sldIdLst>'
        '<p:sldId id="256" r:id="rIdS3"/><p:sldId id="257" r:id="rIdS1"/>'
        '<p:sldId id="258" r:id="rIdS2"/><p:sldId id="259" r:id="rIdS4"/>'
        f'</p:sldIdLst><p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}"/></p:presentation>'
    )
    content_types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="mp4" ContentType="video/mp4"/>'
        '<Default Extension="wav" ContentType="audio/wav"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Override PartName="/ppt/media/media2.m4a" ContentType="audio/mp4"/>'
        "</Types>"
    )
    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": _rels(("rId1", f"{R}/officeDocument", "ppt/presentation.xml")),
        "ppt/presentation.xml": presentation,
        "ppt/_rels/presentation.xml.rels": _rels(
            *((f"rIdS{n}", f"{R}/slide", f"slides/slide{n}.xml") for n in range(1, 5))
        ),
        "ppt/media/media1.mp4": b"video-bytes",
        "ppt/media/media2.m4a": b"audio-bytes",
        "ppt/media/click.wav": b"wav-bytes",
        "ppt/media/image1.png": b"png-bytes",
        **slides,
        **slide_rels,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
        for name, payload in parts.items():
            package.writestr(name, payload)
    return output.getvalue()


def _xml(package: zipfile.ZipFile, name: str) -> etree._Element:
    return etree.fromstring(package.read(name))


def test_references_follow_slide_list_order_and_carry_regions() -> None:
    separated = separate_embedded_media(_build_package(), source_name="deck.pptx")

    summary = [
        (ref.slide_number, ref.shape_id, ref.kind, ref.origin, ref.part_name, ref.content_type)
        for ref in separated.references
    ]
    assert summary == [
        (1, "6", "video", "embedded", "ppt/media/media1.mp4", "video/mp4"),
        (2, "9", "audio", "embedded", "ppt/media/media2.m4a", "audio/mp4"),
        (3, "4", "video", "linked", None, None),
        (4, "sound-1", "audio", "embedded", "ppt/media/click.wav", "audio/wav"),
    ]
    by_slide = {ref.slide_number: ref for ref in separated.references}
    assert by_slide[1].region == (0.0, 0.0, 0.5, 0.5)
    # The group maps its full-slide child space onto a half-size box at (0.1, 0.1).
    assert by_slide[2].region == pytest.approx((0.1, 0.1, 0.35, 0.35))
    assert by_slide[3].external_url == "https://example.com/talk.mp4"
    assert by_slide[4].region is None
    assert by_slide[1].title == "Demo"
    assert by_slide[1].asset_id.startswith("asset_")
    assert separated.hidden_slide_numbers == (2,)
    assert set(separated.media_parts) == {
        "ppt/media/media1.mp4",
        "ppt/media/media2.m4a",
        "ppt/media/click.wav",
    }


def test_slim_package_strips_media_and_keeps_posters() -> None:
    separated = separate_embedded_media(_build_package(), source_name="deck.pptx")

    with zipfile.ZipFile(io.BytesIO(separated.package_bytes)) as slim:
        names = set(slim.namelist())
        assert "ppt/media/media1.mp4" not in names
        assert "ppt/media/media2.m4a" not in names
        # Sounds stay, because their timing trees are not rewritten.
        assert "ppt/media/click.wav" in names
        assert "ppt/media/image1.png" in names
        assert b"media2.m4a" not in slim.read("[Content_Types].xml")

        video_slide = slim.read("ppt/slides/slide3.xml")
        for removed in (b"videoFile", b"p14:media", b"<p:video", b"ppaction://media"):
            assert removed not in video_slide
        assert b'r:embed="rIdPoster"' in video_slide
        video_rels = _xml(slim, "ppt/slides/_rels/slide3.xml.rels")
        assert [rel.get("Id") for rel in video_rels] == ["rIdPoster"]

        assert b'show="0"' not in slim.read("ppt/slides/slide1.xml")
        assert b"audioFile" not in slim.read("ppt/slides/slide1.xml")
        assert b"<p:snd" in slim.read("ppt/slides/slide4.xml")


def test_media_part_still_targeted_elsewhere_is_kept() -> None:
    package = _build_package()
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package)) as source, zipfile.ZipFile(rebuilt, "w") as target:
        for item in source.infolist():
            target.writestr(item, source.read(item.filename))
        target.writestr(
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
            _rels(("rId1", MEDIA_REL, "../media/media1.mp4")),
        )

    separated = separate_embedded_media(rebuilt.getvalue(), source_name="deck.pptx")

    with zipfile.ZipFile(io.BytesIO(separated.package_bytes)) as slim:
        assert "ppt/media/media1.mp4" in slim.namelist()


def test_package_without_media_or_hidden_slides_is_unchanged() -> None:
    package = _build_package()
    plain = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package)) as source, zipfile.ZipFile(plain, "w") as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename.startswith("ppt/slides/slide") and item.filename.endswith(".xml"):
                payload = _slide("").encode()
            target.writestr(item, payload)

    separated = separate_embedded_media(plain.getvalue(), source_name="deck.pptx")

    assert separated.references == ()
    assert separated.package_bytes == plain.getvalue()


def test_missing_media_relationship_is_a_separation_error() -> None:
    package = _build_package()
    broken = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package)) as source, zipfile.ZipFile(broken, "w") as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename == "ppt/slides/_rels/slide3.xml.rels":
                payload = _rels(("rIdPoster", IMAGE_REL, "../media/image1.png")).encode()
            target.writestr(item, payload)

    with pytest.raises(EmbeddedMediaSeparationError, match="rId"):
        separate_embedded_media(broken.getvalue(), source_name="deck.pptx")


class _RecordingStorage:
    results_bucket = "results-bucket"

    def __init__(self) -> None:
        self.uploads: dict[str, tuple[bytes, str | None]] = {}

    def build_result_raw_prefix(self, *, job_id: str) -> str:
        return f"results/{job_id}/"

    def upload_fileobj(
        self, file_obj: Any, storage_key: str, *, bucket: str, content_type: str | None = None
    ) -> dict[str, Any]:
        assert bucket == self.results_bucket
        self.uploads[storage_key] = (file_obj.read(), content_type)
        return {"key": storage_key}


@pytest.fixture
def probe_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        presentation_media,
        "probe_media",
        lambda _path: MediaInfo(duration_ms=65_000, has_audio=True, codec="aac", channels=2),
    )


class _RecordingTranscriber:
    def __init__(self, calls: list[tuple[str, str]], checksum: str) -> None:
        self._calls = calls
        self._checksum = checksum

    def transcribe(
        self,
        audio: MediaAudio,
        *,
        diarize: bool,
        language_hint: str | None,
        usage_task: str,
    ) -> Transcript:
        assert audio.job_id == "job_1"
        assert diarize is True
        assert language_hint is None
        self._calls.append((self._checksum, usage_task))
        return Transcript(
            sentences=(
                TranscriptSentence(start_ms=0, end_ms=900, text="Welcome", speaker_id="0"),
            )
        )


@pytest.fixture
def media_reading(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake audio/frame extraction and vision; record what was asked."""
    recorded: dict[str, Any] = {"asr": [], "frames": [], "vision": []}

    def extract_audio(file_path: str, output_dir: str) -> str:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        return str(Path(output_dir) / "source_audio.wav")

    def extract_frame(file_path: str, output_dir: str) -> str:
        recorded["frames"].append(Path(file_path).read_bytes())
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        return str(Path(output_dir) / "first_frame.png")

    def summarize(**kwargs: Any) -> AssetSummary:
        recorded["vision"].append(kwargs)
        return AssetSummary(summary="A product demo screen")

    monkeypatch.setattr(presentation_media, "extract_source_audio", extract_audio)
    monkeypatch.setattr(presentation_media, "extract_first_frame", extract_frame)
    monkeypatch.setattr(summary_engine, "summarize", summarize)
    recorded["factory"] = lambda checksum: _RecordingTranscriber(recorded["asr"], checksum)
    return recorded


def test_presentation_media_is_stored_with_manifest(
    tmp_path: Path, probe_duration: None, media_reading: dict[str, Any]
) -> None:
    source = tmp_path / "deck.pptx"
    source.write_bytes(_build_package())
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    storage = _RecordingStorage()

    separated = separate_presentation_media(
        file_path=str(source),
        filename="deck.pptx",
        job_id="job_1",
        work_dir=str(work_dir),
        asset_model="vision-model",
        storage=storage,  # type: ignore[arg-type]
        transcriber_factory=media_reading["factory"],
    )

    slim_path = separated.conversion_path
    assert slim_path == str(work_dir / "deck.pptx")
    with zipfile.ZipFile(slim_path) as slim:
        assert "ppt/media/media1.mp4" not in slim.namelist()
    manifest_bytes, manifest_type = storage.uploads.pop(
        "results/job_1/embedded_media/manifest.json"
    )
    assert manifest_type == "application/json"
    stored = {key.rsplit("-", 1)[-1]: value for key, value in storage.uploads.items()}
    assert stored == {
        "media1.mp4": (b"video-bytes", "video/mp4"),
        "media2.m4a": (b"audio-bytes", "audio/mp4"),
        "click.wav": (b"wav-bytes", "audio/wav"),
    }
    manifest = json.loads(manifest_bytes)
    assert manifest["hidden_slide_numbers"] == [2]
    first = manifest["assets"][0]
    assert first["kind"] == "video"
    assert first["page_nums"] == [1]
    assert first["region"] == {
        "page_num": 1,
        "bbox": [0.0, 0.0, 0.5, 0.5],
        "coord_system": "page_fraction",
    }
    assert first["duration_ms"] == 65_000
    assert first["size_bytes"] == len(b"video-bytes")
    assert first["ref"]["kind"] == "artifact"
    assert f"results/job_1/{first['ref']['path']}" in storage.uploads
    linked = manifest["assets"][2]
    assert linked["ref"] == {"kind": "external", "url": "https://example.com/talk.mp4"}
    assert linked["duration_ms"] is None
    assert separated.page_media.hidden_pages == frozenset({2})
    assert [asset["asset_id"] for asset in separated.page_media.assets_by_page[1]] == [
        first["asset_id"]
    ]
    # One transcript per distinct part with audio; one frame per video part.
    assert sorted(media_reading["asr"]) == sorted(
        (asset["checksum_sha256"], EMBEDDED_MEDIA_ASR_USAGE_TASK)
        for asset in manifest["assets"]
        if asset["checksum_sha256"]
    )
    assert media_reading["frames"] == [b"video-bytes"]
    [vision_call] = media_reading["vision"]
    assert vision_call["mode"] == "asset"
    assert vision_call["model"] == "vision-model"
    assert vision_call["usage_task"] == EMBEDDED_MEDIA_FRAME_USAGE_TASK
    assert vision_call["asset_title_hint"] == "Demo"
    assert first["summary"] == "A product demo screen"
    assert manifest["assets"][1]["summary"] is None
    assert linked["summary"] is None

    rows = {row["know_id"]: row for row in separated.media_rows}
    assert list(rows) == [asset["asset_id"] for asset in manifest["assets"]]
    video_row = rows[first["asset_id"]]
    assert video_row["type"] == "video"
    assert video_row["path"] == f"embedded_media/{first['asset_id']}"
    assert video_row["page_nums"] == "1"
    assert video_row["asset_title"] == "Demo"
    assert video_row["summary"] == "A product demo screen"
    assert video_row["content"] == (
        f"[video: Demo · 01:05 · asset:{first['asset_id']}]\n\n"
        "A product demo screen\n\n[00:00:00–00:00:00] S1: Welcome"
    )
    assert video_row["length"] == len(video_row["content"])
    assert video_row["extra_metadata"]["asset"]["asset_id"] == first["asset_id"]
    assert video_row["extra_metadata"]["asset"]["summary"] == "A product demo screen"
    linked_row = rows[linked["asset_id"]]
    assert linked_row["content"] == f"[video: Linked · linked · asset:{linked['asset_id']}]"


def test_page_rows_carry_their_media_and_markers() -> None:
    video = {
        "asset_id": "asset_v",
        "kind": "video",
        "origin": "embedded",
        "title": "Demo",
        "page_nums": [2],
        "region": None,
        "duration_ms": 65_000,
        "ref": {"kind": "artifact", "path": "embedded_media/abc-media1.mp4"},
    }
    page_media = PageMedia.from_descriptors([video], [2])
    media_row = {"type": "video", "content": "[video]", "page_nums": "2"}
    rows = pd.DataFrame(
        [
            {"type": "page", "content": "Intro", "page_nums": "1", "length": 5, "extra_metadata": {}},
            {
                "type": "page",
                "content": "Body",
                "page_nums": "2,3",
                "length": 4,
                "connectto": json.dumps([{"target": "img_1", "relation": "embeds"}]),
                "extra_metadata": {"scope_id": "p2-3"},
            },
            {"type": "image", "content": "Chart", "page_nums": "2", "length": 5, "extra_metadata": {}},
        ]
    )

    attached = attach_presentation_media(
        rows,
        PresentationMedia(
            conversion_path="deck.pdf", page_media=page_media, media_rows=(media_row,)
        ),
    ).to_dict("records")

    assert attached[0]["content"] == "Intro"
    assert "assets" not in attached[0]["extra_metadata"]
    assert attached[1]["content"] == "Body\n[video: Demo · 01:05 · asset:asset_v]"
    assert attached[1]["length"] == len(attached[1]["content"])
    assert attached[1]["extra_metadata"]["scope_id"] == "p2-3"
    assert attached[1]["extra_metadata"]["assets"][0]["asset_id"] == "asset_v"
    assert attached[1]["extra_metadata"]["hidden_page_nums"] == [2]
    assert json.loads(attached[1]["connectto"]) == [
        {"target": "img_1", "relation": "embeds"},
        {"target": "asset_v", "relation": "embeds"},
    ]
    assert "assets" not in attached[2]["extra_metadata"]
    assert len(attached) == 4
    assert attached[3]["type"] == "video"
    assert attached[3]["content"] == "[video]"


def test_identical_media_bytes_are_stored_once(
    tmp_path: Path, probe_duration: None, media_reading: dict[str, Any]
) -> None:
    package = _build_package()
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package)) as source, zipfile.ZipFile(rebuilt, "w") as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename == "ppt/media/media2.m4a":
                payload = b"video-bytes"
            target.writestr(item, payload)
    source_path = tmp_path / "deck.pptx"
    source_path.write_bytes(rebuilt.getvalue())
    storage = _RecordingStorage()

    separate_presentation_media(
        file_path=str(source_path),
        filename="deck.pptx",
        job_id="job_1",
        work_dir=str(tmp_path),
        asset_model=None,
        storage=storage,  # type: ignore[arg-type]
        transcriber_factory=media_reading["factory"],
    )

    media_keys = [key for key in storage.uploads if not key.endswith("manifest.json")]
    assert len(media_keys) == 2
    assert len(media_reading["asr"]) == 2


def test_presentation_without_media_converts_the_original(tmp_path: Path) -> None:
    package = _build_package()
    plain = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package)) as source, zipfile.ZipFile(plain, "w") as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename.startswith("ppt/slides/slide") and item.filename.endswith(".xml"):
                payload = _slide("").encode()
            target.writestr(item, payload)
    source_path = tmp_path / "deck.pptx"
    source_path.write_bytes(plain.getvalue())
    storage = _RecordingStorage()

    result = separate_presentation_media(
        file_path=str(source_path),
        filename="deck.pptx",
        job_id=None,
        work_dir=str(tmp_path),
        asset_model=None,
        storage=storage,  # type: ignore[arg-type]
    )

    assert result.conversion_path == str(source_path)
    assert result.page_media.is_empty
    assert storage.uploads == {}


def test_unreadable_media_fails_the_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_probe(_path: str) -> MediaInfo:
        raise ValidationException(
            user_message="Could not read this media file.",
            violations=[{"field": "file", "description": "ffprobe failed"}],
        )

    monkeypatch.setattr(presentation_media, "probe_media", fail_probe)
    source_path = tmp_path / "deck.pptx"
    source_path.write_bytes(_build_package())

    with pytest.raises(PresentationParsingException) as raised:
        separate_presentation_media(
            file_path=str(source_path),
            filename="deck.pptx",
            job_id="job_1",
            work_dir=str(tmp_path),
            asset_model=None,
            storage=_RecordingStorage(),  # type: ignore[arg-type]
        )

    assert raised.value.details["reason"] == EMBEDDED_MEDIA_SEPARATION_FAILED


def test_broken_package_fails_the_parse(tmp_path: Path) -> None:
    source_path = tmp_path / "deck.pptx"
    source_path.write_bytes(b"not a zip")

    with pytest.raises(PresentationParsingException) as raised:
        separate_presentation_media(
            file_path=str(source_path),
            filename="deck.pptx",
            job_id="job_1",
            work_dir=str(tmp_path),
            asset_model=None,
            storage=_RecordingStorage(),  # type: ignore[arg-type]
        )

    assert raised.value.details == {
        "file_type": "pptx",
        "reason": EMBEDDED_MEDIA_SEPARATION_FAILED,
    }


def test_silent_video_is_described_without_transcription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, media_reading: dict[str, Any]
) -> None:
    monkeypatch.setattr(
        presentation_media,
        "probe_media",
        lambda _path: MediaInfo(duration_ms=4_000, has_audio=False, codec=None, channels=None),
    )
    source = tmp_path / "deck.pptx"
    source.write_bytes(_build_package())

    separated = separate_presentation_media(
        file_path=str(source),
        filename="deck.pptx",
        job_id="job_1",
        work_dir=str(tmp_path),
        asset_model=None,
        storage=_RecordingStorage(),  # type: ignore[arg-type]
        transcriber_factory=media_reading["factory"],
    )

    assert media_reading["asr"] == []
    video_row = separated.media_rows[0]
    assert video_row["content"].endswith("A product demo screen")
    assert separated.media_rows[1]["summary"] == ""


def test_unreadable_first_frame_fails_the_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_duration: None,
    media_reading: dict[str, Any],
) -> None:
    def no_frame(file_path: str, output_dir: str) -> str:
        raise ValidationException(
            user_message="Could not read a frame from this video.",
            violations=[{"field": "file", "description": "no frame"}],
        )

    monkeypatch.setattr(presentation_media, "extract_first_frame", no_frame)
    source = tmp_path / "deck.pptx"
    source.write_bytes(_build_package())
    storage = _RecordingStorage()

    with pytest.raises(PresentationParsingException) as raised:
        separate_presentation_media(
            file_path=str(source),
            filename="deck.pptx",
            job_id="job_1",
            work_dir=str(tmp_path),
            asset_model=None,
            storage=storage,  # type: ignore[arg-type]
            transcriber_factory=media_reading["factory"],
        )

    assert raised.value.details["reason"] == EMBEDDED_MEDIA_SEPARATION_FAILED
    assert media_reading["vision"] == []
    assert storage.uploads == {}


def test_empty_frame_description_is_not_stored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_duration: None,
    media_reading: dict[str, Any],
) -> None:
    monkeypatch.setattr(summary_engine, "summarize", lambda **_kwargs: AssetSummary())
    source = tmp_path / "deck.pptx"
    source.write_bytes(_build_package())

    separated = separate_presentation_media(
        file_path=str(source),
        filename="deck.pptx",
        job_id="job_1",
        work_dir=str(tmp_path),
        asset_model=None,
        storage=_RecordingStorage(),  # type: ignore[arg-type]
        transcriber_factory=media_reading["factory"],
    )

    assert separated.media_rows[0]["extra_metadata"]["asset"]["summary"] is None


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is required to render a frame"
)
def test_first_frame_is_written_as_png(tmp_path: Path) -> None:
    from app.services.document_parser.formats.media.extract import extract_first_frame

    video = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=64x48:rate=5:duration=1",
            "-pix_fmt", "yuv420p", str(video),
        ],
        check=True,
    )

    frame = Path(extract_first_frame(str(video), str(tmp_path / "frame")))

    assert frame.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    not_video = tmp_path / "noise.mp4"
    not_video.write_bytes(b"not a video")
    with pytest.raises(ValidationException):
        extract_first_frame(str(not_video), str(tmp_path / "noise"))


def test_media_shape_without_a_relationship_fails_separation() -> None:
    package = _build_package()
    broken = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package)) as source, zipfile.ZipFile(broken, "w") as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename == "ppt/slides/slide2.xml":
                payload = _slide(
                    _media_picture(4, "Linked", "videoFile", "", None, _xfrm(0, 0, 1, 1))
                ).encode()
            target.writestr(item, payload)

    with pytest.raises(EmbeddedMediaSeparationError, match="no media relationship"):
        separate_embedded_media(broken.getvalue(), source_name="deck.pptx")
