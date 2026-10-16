from __future__ import annotations

import copy
import json
import pickle
import traceback
from pathlib import Path
import warnings
import zipfile

import pytest
from jsonschema import Draft202012Validator
from loguru import logger

from shared.contracts.parse_result import (
    validate_parse_result,
    validate_parse_result_archive,
)
from shared.core.exceptions.domain_exceptions import ParseResultContractException

FIXTURES = Path(__file__).parent / "fixtures" / "parse_result"
ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def payloads():
    return [
        json.loads((FIXTURES / f"{name}.json").read_text())
        for name in ("manifest", "chunks", "doc_nav")
    ]


def _archive(path, payloads, extras=None):
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in zip(
            ("manifest.json", "chunks.json", "doc_nav.json"), payloads
        ):
            if payload is not None:
                archive.writestr(name, json.dumps(payload))
        for name in (
            "images/picture.png",
            "tables/table.html",
            "page_citation_assets/page-3.webp",
        ):
            archive.writestr(name, b"synthetic asset")
        for name, content in extras or []:
            archive.writestr(name, content)
    return str(path)


def test_real_v1_shapes_and_generated_schemas(payloads, tmp_path):
    result = validate_parse_result(*payloads, allow_legacy=False)
    assert [chunk.type for chunk in result.chunks.chunks] == [
        "text",
        "image",
        "table",
        "page",
    ]
    assert result.chunks.chunks[2].content == "tables/table.html"
    assert result.doc_nav.model_extra["top_summary"].startswith("Preserve")
    assert result.warnings == ()
    for name, payload in zip(("manifest", "chunks", "doc_nav"), payloads):
        schema = json.loads(
            (
                ROOT / "packages" / "contracts" / "parse_result" / f"{name}.schema.json"
            ).read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)
    assert (
        validate_parse_result_archive(
            _archive(tmp_path / "result.zip", payloads), allow_legacy=False
        ).warnings
        == ()
    )


def test_legacy_versions_and_navigation_omission(payloads):
    for payload in payloads:
        payload.pop("schema_version")
    result = validate_parse_result(*payloads)
    assert len(result.warnings) == 3
    assert all("schema_version" not in payload for payload in payloads)
    assert validate_parse_result(payloads[0], payloads[1], None).doc_nav is None
    with pytest.raises(ParseResultContractException):
        validate_parse_result(*payloads, allow_legacy=False)


@pytest.mark.parametrize("version", [2, "1", True, None])
def test_unsupported_versions_do_not_coerce(payloads, version):
    payloads[1]["schema_version"] = version
    with pytest.raises(ParseResultContractException) as error:
        validate_parse_result(*payloads)
    assert error.value.details["violations"] == [
        {
            "artifact": "chunks.json",
            "field": "schema_version",
            "reason": "unsupported_schema_version",
        }
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("content", {"secret": "private customer content"}),
        ("type", "unknown"),
        ("metadata", None),
        ("chunk_id", ""),
    ],
)
def test_malformed_chunk_fields_are_sanitized(payloads, field, value):
    payloads[1]["chunks"][0][field] = value
    with pytest.raises(ParseResultContractException) as error:
        validate_parse_result(*payloads)
    client = error.value.to_client("request")
    assert client["error"]["details"]["schema_version"] == 1
    assert "private customer content" not in json.dumps(client)
    assert "private customer content" not in "".join(traceback.format_exception(error.value))
    assert "input" not in json.dumps(client)
    assert pickle.loads(pickle.dumps(error.value)).details == error.value.details


@pytest.mark.parametrize("field", ["length", "summary", "page_nums"])
def test_required_metadata_and_strict_integer(payloads, field):
    del payloads[1]["chunks"][0]["metadata"][field]
    with pytest.raises(ParseResultContractException):
        validate_parse_result(*payloads)
    payloads = [
        json.loads((FIXTURES / f"{name}.json").read_text())
        for name in ("manifest", "chunks", "doc_nav")
    ]
    payloads[1]["chunks"][0]["metadata"]["length"] = "5"
    with pytest.raises(ParseResultContractException):
        validate_parse_result(*payloads)


def test_counts_and_connection_span_are_consistent(payloads):
    invalid = copy.deepcopy(payloads)
    invalid[0]["statistics"]["total_chunks"] = 3
    with pytest.raises(ParseResultContractException, match="validation failed"):
        validate_parse_result(*invalid)
    payloads[1]["chunks"][0]["metadata"]["connect_to"] = [
        {"target": "image-1", "relation": "embeds", "position": {"start": 7, "end": 3}}
    ]
    with pytest.raises(ParseResultContractException):
        validate_parse_result(*payloads)


@pytest.mark.parametrize(
    "extra,reason",
    [
        (("../escape", "x"), "unsafe_member"),
        (("chunks.json", "{}"), "duplicate_member"),
        (("/absolute", "x"), "unsafe_member"),
        (("images\\escape", "x"), "unsafe_member"),
    ],
)
def test_invalid_archive_members(payloads, tmp_path, extra, reason):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        path = _archive(tmp_path / "bad.zip", payloads, [extra])
    with pytest.raises(ParseResultContractException) as error:
        validate_parse_result_archive(path)
    assert error.value.details["violations"][0]["reason"] == reason


def test_archive_requires_json_and_referenced_assets(payloads, tmp_path):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(payloads[0]))
        archive.writestr("chunks.json", "{invalid")
    with pytest.raises(ParseResultContractException) as error:
        validate_parse_result_archive(str(path))
    assert error.value.details["violations"][0]["reason"] == "invalid_json"
    payloads[1]["chunks"][1]["metadata"]["file_path"] = "images/absent.png"
    with pytest.raises(ParseResultContractException) as error:
        validate_parse_result_archive(_archive(path, payloads))
    assert error.value.details["violations"][0]["reason"] == "referenced_asset_missing"
    payloads[1]["chunks"][1]["metadata"]["file_path"] = "images/../secret"
    with pytest.raises(ParseResultContractException):
        validate_parse_result(*payloads)


def test_raw_nul_member_name_is_not_hidden_by_zip_normalization(payloads, tmp_path):
    path = tmp_path / "nul-name.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in zip(("manifest", "chunks", "doc_nav"), payloads):
            member = "chunks.json!hidden" if name == "chunks" else f"{name}.json"
            archive.writestr(member, json.dumps(payload))
        for name in (
            "images/picture.png", "tables/table.html", "page_citation_assets/page-3.webp"
        ):
            archive.writestr(name, b"synthetic asset")
    path.write_bytes(
        path.read_bytes().replace(b"chunks.json!hidden", b"chunks.json\0hidden")
    )
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("chunks.json")
        assert "\0" in info.orig_filename and "\0" not in info.filename
    with pytest.raises(ParseResultContractException) as error:
        validate_parse_result_archive(str(path), allow_legacy=False)
    assert error.value.details["violations"][0]["reason"] == "unsafe_member"


def test_new_versioned_archive_requires_nav_and_bounded_json(payloads, tmp_path):
    with pytest.raises(ParseResultContractException):
        validate_parse_result_archive(
            _archive(tmp_path / "missing.zip", [*payloads[:2], None])
        )
    with pytest.raises(ParseResultContractException) as error:
        validate_parse_result_archive(
            _archive(tmp_path / "oversized.zip", payloads), max_json_bytes=1
        )
    assert error.value.details["violations"][0]["reason"] == "json_size_limit"


def test_malformed_hierarchy_does_not_expose_customer_headings(payloads):
    payloads[0]["HIERARCHY"] = {"private customer heading": []}
    with pytest.raises(ParseResultContractException) as error:
        validate_parse_result(*payloads)
    assert "private customer heading" not in json.dumps(
        error.value.to_client("request")
    )
    assert error.value.details["violations"][0]["field"] == "HIERARCHY"


def test_canonical_logging_does_not_render_document_locals(payloads):
    secret = "private-document-content-in-diagnostic-locals"
    payloads[1]["chunks"][0]["content"] = {"secret": secret}
    chunks = {"customer_content": secret, **payloads[1]}
    messages = []
    sink = logger.add(
        messages.append,
        format="{extra} {message}",
        diagnose=True,
        backtrace=True,
    )
    try:
        with pytest.raises(ParseResultContractException) as error:
            validate_parse_result(payloads[0], chunks, payloads[2])
        error.value.logging(job_id="synthetic-job")
    finally:
        logger.remove(sink)

    assert len(messages) == 1
    assert secret not in str(messages[0])
    assert messages[0].record["exception"] is None
    extra = messages[0].record["extra"]
    assert extra["event"] == "exception.system"
    assert extra["job_id"] == "synthetic-job"
    assert extra["details"]["reason"] == "PARSE_RESULT_CONTRACT_VIOLATION"


@pytest.mark.parametrize(
    "content", ['{"schema_version": 1, "schema_version": 2}', '{"schema_version": NaN}']
)
def test_ambiguous_or_nonfinite_json_rejected(payloads, tmp_path, content):
    path = tmp_path / "invalid.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", content)
        archive.writestr("chunks.json", json.dumps(payloads[1]))
    with pytest.raises(ParseResultContractException) as error:
        validate_parse_result_archive(str(path))
    assert error.value.details["violations"][0]["reason"] == "invalid_json"


def test_unsupported_compression_has_structured_error(payloads, tmp_path):
    import struct

    path = tmp_path / "compression.zip"
    _archive(path, payloads)
    data = bytearray(path.read_bytes())
    local = data.index(b"PK\x03\x04")
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<H", data, local + 8, 99)
    struct.pack_into("<H", data, central + 10, 99)
    path.write_bytes(data)
    with pytest.raises(ParseResultContractException) as error:
        validate_parse_result_archive(str(path))
    assert error.value.details["violations"][0]["reason"] == "unreadable_archive"


def test_versioned_chunks_do_not_use_legacy_navigation_omission(payloads):
    payloads[0].pop("schema_version")
    with pytest.raises(ParseResultContractException) as error:
        validate_parse_result(payloads[0], payloads[1], None)
    assert error.value.details["violations"][0]["reason"] == "required_artifact_missing"


def test_numeric_overflow_cannot_bypass_finite_json_contract(payloads, tmp_path):
    path = tmp_path / "overflow.zip"
    manifest = json.dumps(payloads[0]).replace('"credits": null', '"credits": 1e400')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("chunks.json", json.dumps(payloads[1]))
    with pytest.raises(ParseResultContractException) as error:
        validate_parse_result_archive(str(path))
    assert error.value.details["violations"][0]["reason"] == "invalid_json"
