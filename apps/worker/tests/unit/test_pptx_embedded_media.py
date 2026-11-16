from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from app.services.document_parser.formats.media.probe import MediaInfo
from app.services.document_parser.formats.pptx.embedded_media import (
    EmbeddedMediaSeparationError,
    separate_embedded_media,
)
from app.services.page_memory import presentation_media
from app.services.page_memory.presentation_media import (
    EMBEDDED_MEDIA_SEPARATION_FAILED,
    separate_presentation_media,
)

from shared.core.exceptions.domain_exceptions import (
    PresentationParsingException,
    ValidationException,
)

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


def test_presentation_media_is_stored_with_manifest(tmp_path: Path, probe_duration: None) -> None:
    source = tmp_path / "deck.pptx"
    source.write_bytes(_build_package())
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    storage = _RecordingStorage()

    slim_path = separate_presentation_media(
        file_path=str(source),
        filename="deck.pptx",
        job_id="job_1",
        work_dir=str(work_dir),
        storage=storage,  # type: ignore[arg-type]
    )

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


def test_identical_media_bytes_are_stored_once(tmp_path: Path, probe_duration: None) -> None:
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
        storage=storage,  # type: ignore[arg-type]
    )

    media_keys = [key for key in storage.uploads if not key.endswith("manifest.json")]
    assert len(media_keys) == 2


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
        storage=storage,  # type: ignore[arg-type]
    )

    assert result == str(source_path)
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
            storage=_RecordingStorage(),  # type: ignore[arg-type]
        )

    assert raised.value.details == {
        "file_type": "pptx",
        "reason": EMBEDDED_MEDIA_SEPARATION_FAILED,
    }
