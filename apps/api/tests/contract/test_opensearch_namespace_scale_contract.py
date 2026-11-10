"""Check a frozen full namespace and candidate depth on an owned native node.

KNOWHERE_SCALE_FIXTURE is read-only. Set KNOWHERE_SCALE_OUTPUT to a new report
file outside that resolved fixture directory, for example dev/results/scale.json.
"""

from __future__ import annotations

from collections.abc import Iterator
from hashlib import sha256
import gzip
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from pydantic import BaseModel, Field
import pytest


class _Query(BaseModel):
    id: str
    text: str
    tokens: tuple[str, ...]
    groups: tuple[frozenset[str], ...]
    pgTop10: tuple[str, ...]
    pgEvidence: bool


class _Prepared(BaseModel):
    unitCount: int
    documentCount: int
    postingCount: int
    frequencySums: tuple[int, int]
    sourceHashes: dict[str, str]
    builds: dict[str, str]
    targetDocument: str
    queries: tuple[_Query, ...]
    nativeInputSha256: str
    pgReference: str


class _Count(BaseModel):
    count: int


class _ShardResult(BaseModel):
    failed: int


class _BulkItem(BaseModel):
    status: int


class _BulkResponse(BaseModel):
    errors: bool
    items: tuple[dict[str, _BulkItem], ...]


def _resolveScaleOutput(directory: Path, *, configuredPath: str | None) -> Path:
    if not configuredPath:
        raise ValueError("Set KNOWHERE_SCALE_OUTPUT to a new owned report outside the immutable fixture")
    output: Path = Path(configuredPath).resolve()
    if output.is_relative_to(directory.resolve()):
        raise ValueError("Scale report output must be outside the immutable fixture directory")
    if output.exists():
        raise ValueError("Scale report output already exists; choose a new owned result file")
    if not output.parent.is_dir():
        raise ValueError("Create the owned scale report directory before execution")
    return output


@pytest.mark.skipif(not os.environ.get("KNOWHERE_SCALE_FIXTURE"),
    reason="An explicitly prepared real namespace and owned node are required")
def test_full_namespace_preserves_scope_and_qualifies_candidate_depth() -> None:
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.hit import LexicalHit
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.searchplan import NativeSearchPlan
    from shared.services.retrieval.lexical.unit import LexicalUnit

    class StoredUnit(LexicalUnit):
        buildId: str

    class PageHit(BaseModel):
        identity: str = Field(alias="_id")
        score: float = Field(alias="_score")
        source: StoredUnit = Field(alias="_source")
        sort: tuple[str | float | int, ...]

    class HitTotal(BaseModel):
        value: int
        relation: str

    class PageHits(BaseModel):
        hits: tuple[PageHit, ...]
        total: HitTotal

    class Page(BaseModel):
        hits: PageHits
        timed_out: bool
        shards: _ShardResult = Field(alias="_shards")

    directory: Path = Path(os.environ["KNOWHERE_SCALE_FIXTURE"])
    outputPath: Path = _resolveScaleOutput(directory, configuredPath=os.environ.get("KNOWHERE_SCALE_OUTPUT"))
    endpoint: str = os.environ["KNOWHERE_TEST_OPENSEARCH_URL"]
    assert urlsplit(endpoint).hostname in ("127.0.0.1", "::1")
    prepared: _Prepared = _Prepared.model_validate_json((directory / "prepared.private.json").read_bytes())
    assert prepared.unitCount == 42794 and prepared.documentCount == 644
    assert len(prepared.queries) == 20
    inputPath: Path = directory / "native-units.private.jsonl.gz"
    assert sha256(inputPath.read_bytes()).hexdigest() == prepared.nativeInputSha256
    native: OpenSearchLexicalIndex = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="scale-contract", namespace=uuid4().hex),
        indexPrefix="knowhere-scale-s8m4")
    activeBuilds: tuple[str, ...] = tuple(prepared.builds.values())
    metadata: dict[tuple[str, str], dict[str, object]] = {}
    target: list[LexicalUnit] = []
    samples: list[LexicalUnit] = []
    report: dict[str, object] = {"unitCount": prepared.unitCount,
        "documentCount": prepared.documentCount, "postingCount": prepared.postingCount,
        "sourceHashes": prepared.sourceHashes, "pgReference": prepared.pgReference,
        "candidateDepths": [100, 1000, "all-matches"], "queries": [], "checks": {}}

    def saveReport() -> None:
        outputPath.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def readUnits() -> Iterator[StoredUnit]:
        with gzip.open(inputPath, "rt") as stream:
            for line in stream:
                yield StoredUnit.model_validate_json(line)

    def countRecords() -> int:
        body: object = native._request("POST", f"/{native.indexName}/_count", payload={"query": {"match_all": {}}})
        return _Count.model_validate(body).count

    def loadBatch(batch: list[StoredUnit]) -> None:
        lines: list[str] = []
        for unit in batch:
            identity: str = sha256(json.dumps([unit.buildId, unit.unitId], separators=(",", ":")).encode()).hexdigest()
            lines.extend((json.dumps({"index": {"_id": identity}}), unit.model_dump_json()))
        response: _BulkResponse = _BulkResponse.model_validate(native._request("POST",
            f"/{native.indexName}/_bulk?refresh=false", textBody="\n".join(lines) + "\n"))
        assert not response.errors and len(response.items) == len(batch)
        assert all(200 <= item.status < 300 for operation in response.items for item in operation.values())

    def collectChannel(channel: str, query: _Query) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        after: tuple[str | float | int, ...] | None = None
        total: int = -1
        while True:
            payload: dict[str, object] = NativeSearchPlan.buildPayload(channel,
                buildIds=activeBuilds, queryTokens=query.tokens, limit=1000, unitIds=None)
            payload["track_total_hits"] = True
            if after is not None:
                payload["search_after"] = list(after)
            page: Page = Page.model_validate(native._request("POST",
                f"/{native.indexName}/_search?allow_partial_search_results=false", payload=payload))
            assert not page.timed_out and page.shards.failed == 0
            assert page.hits.total.relation == "eq"
            assert total in (-1, page.hits.total.value)
            total = page.hits.total.value
            for hit in page.hits.hits:
                assert not {"pathTokens", "contentTokens"} & hit.source.model_fields_set
                assert hit.source.model_dump(exclude={"pathTokens", "contentTokens"}) == metadata[
                    hit.source.buildId, hit.source.unitId]
                result.append(hit.model_dump(by_alias=True))
            if len(page.hits.hits) < 1000:
                break
            after = page.hits.hits[-1].sort
        assert len(result) == total
        return result

    def fuse(channels: tuple[list[dict[str, object]], list[dict[str, object]]],
             depth: int | None) -> tuple[LexicalHit, ...]:
        pathRows, contentRows = channels
        bodies: tuple[object, object] = (
            {"timed_out": False, "_shards": {"failed": 0},
                "hits": {"hits": pathRows[:depth] if depth is not None else pathRows}},
            {"timed_out": False, "_shards": {"failed": 0},
                "hits": {"hits": contentRows[:depth] if depth is not None else contentRows}},
        )
        return NativeSearchPlan.fuseChannels(bodies, buildIds=activeBuilds, limit=10)

    native.createIndex()
    try:
        batch: list[StoredUnit] = []
        loaded: int = 0
        for unit in readUnits():
            metadata[unit.buildId, unit.unitId] = unit.model_dump(exclude={"pathTokens", "contentTokens"})
            plain: LexicalUnit = LexicalUnit.model_validate(unit.model_dump(exclude={"buildId"}))
            if unit.documentId == prepared.targetDocument:
                target.append(plain)
            if len(samples) < 2500:
                samples.append(plain)
            batch.append(unit)
            if len(batch) == 200:
                loadBatch(batch)
                loaded += len(batch)
                batch = []
                if loaded % 5000 == 0:
                    print(f"loaded real namespace units={loaded}", flush=True)
        if batch:
            loadBatch(batch)
        native._request("POST", f"/{native.indexName}/_refresh")
        assert countRecords() == prepared.unitCount
        assert target
        report["loadedCount"] = countRecords()
        originalUuid: str = native.getIndexUuid()
        queryRows: list[dict[str, object]] = []
        lostByDepth: dict[str, list[str]] = {"100": [], "1000": [], "all-matches": []}
        for query in prepared.queries:
            channels: tuple[list[dict[str, object]], list[dict[str, object]]] = (
                collectChannel("pathTokens", query), collectChannel("contentTokens", query))
            outcomes: dict[str, object] = {}
            for name, depth in (("100", 100), ("1000", 1000), ("all-matches", None)):
                hits: tuple[LexicalHit, ...] = fuse(channels, depth)
                identities: list[str] = [hit.unit.unitId for hit in hits]
                hasEvidence: bool = all(group.intersection(identities) for group in query.groups)
                if query.pgEvidence and not hasEvidence:
                    lostByDepth[name].append(query.id)
                outcomes[name] = {"unitIds": identities, "hasEvidence": hasEvidence}
            production: tuple[LexicalHit, ...] = native.search(
                buildIds=activeBuilds, queryTokens=query.tokens, limit=10)
            assert production == fuse(channels, 100)
            queryRows.append({"id": query.id, "channelMatchCounts": [len(rows) for rows in channels],
                "pgTop10": query.pgTop10, "pgEvidence": query.pgEvidence, "native": outcomes})
            report["queries"] = queryRows
            report["lostPGEvidenceByDepth"] = lostByDepth
            saveReport()
            print(f"qualified candidate depths query={query.id} channel_matches={[len(rows) for rows in channels]}", flush=True)

        oldBuild: str = prepared.builds[prepared.targetDocument]
        newBuild: str = str(uuid5(NAMESPACE_URL, "scale-s8m4-replacement-build"))
        newRevision: str = str(uuid5(NAMESPACE_URL, "scale-s8m4-replacement-revision"))
        marker: str = "namespaceversionmarkers8m4"
        replacement: tuple[LexicalUnit, ...] = tuple(unit.model_copy(update={
            "revisionId": newRevision,
            "sectionId": "sec_" + sha256((newRevision + unit.sectionId).encode()).hexdigest()[:12],
            "contentTokens": unit.contentTokens + " " + marker}) for unit in target)
        native.writeBuild(buildId=newBuild, units=replacement)
        assert countRecords() == prepared.unitCount + len(target)
        assert not native.search(buildIds=activeBuilds, queryTokens=(marker,), limit=10)
        staged: tuple[LexicalHit, ...] = native.search(buildIds=(newBuild,), queryTokens=(marker,), limit=10)
        assert staged and all(hit.unit.revisionId == newRevision and hit.unit.documentId == prepared.targetDocument for hit in staged)
        activeBuilds = tuple(newBuild if identity == oldBuild else identity for identity in activeBuilds)
        assert native.deleteBuild(oldBuild)
        assert countRecords() == prepared.unitCount
        assert not native.search(buildIds=(oldBuild,), queryTokens=prepared.queries[0].tokens, limit=10)
        assert native.search(buildIds=activeBuilds, queryTokens=(marker,), limit=10)
        native.setIndexOpen(isOpen=False, operationId=uuid4().hex, timeoutSeconds=15)
        with pytest.raises(LexicalIndexError) as closed:
            native.search(buildIds=activeBuilds, queryTokens=(marker,), limit=1)
        assert closed.value.isIndexClosed
        native.setIndexOpen(isOpen=True, operationId=uuid4().hex, timeoutSeconds=15)
        assert native.getIndexUuid() == originalUuid
        assert countRecords() == prepared.unitCount
        assert native.search(buildIds=activeBuilds, queryTokens=(marker,), limit=1)

        paddingCount: int = 45000 - countRecords()
        paddingBuild: str = str(uuid5(NAMESPACE_URL, "scale-s8m4-padding-build"))
        paddingRevision: str = str(uuid5(NAMESPACE_URL, "scale-s8m4-padding-revision"))
        paddingMarker: str = "namespacepaddingmarkers8m4"
        padding: tuple[LexicalUnit, ...] = tuple(samples[position % len(samples)].model_copy(update={
            "documentId": "scale-padding", "revisionId": paddingRevision,
            "unitId": str(uuid5(NAMESPACE_URL, f"scale-s8m4-padding-{position}")),
            "sectionId": f"pad-{position}", "contentTokens": samples[position % len(samples)].contentTokens + " " + paddingMarker,
        }) for position in range(paddingCount))
        native.writeBuild(buildId=paddingBuild, units=padding)
        assert countRecords() == 45000
        assert native.search(buildIds=(paddingBuild,), queryTokens=(paddingMarker,), limit=10)
        assert not native.search(buildIds=activeBuilds, queryTokens=(paddingMarker,), limit=10)
        report["checks"] = {"realCorpusLoaded": True, "unactivatedBuildExcluded": True,
            "replacementRevisionSelected": True, "oldBuildCleanup": True,
            "closedReadRejected": True, "reopenUuidAndDataPreserved": True,
            "derived45000Count": 45000, "derivedPaddingUnits": paddingCount,
            "derivedPaddingExcludedFromPublishedScope": True}
        report["relativeEvidencePassed"] = not lostByDepth["100"]
        saveReport()
        assert not lostByDepth["100"], f"Default candidate depth lost PG evidence: {lostByDepth}"
    finally:
        saveReport()
        response: httpx.Response = httpx.delete(f"{endpoint}/{native.indexName}", timeout=30, trust_env=False)
        assert response.status_code in (200, 404)
        native.close()


def test_scale_report_requires_explicit_owned_output(tmp_path: Path) -> None:
    fixture: Path = tmp_path / "fixture"
    fixture.mkdir()
    with pytest.raises(ValueError, match="KNOWHERE_SCALE_OUTPUT"):
        _resolveScaleOutput(fixture, configuredPath=None)
    with pytest.raises(ValueError, match="fixture"):
        _resolveScaleOutput(fixture, configuredPath=str(fixture / "namespace-result.private.json"))


def test_scale_report_rejects_fixture_symlink_and_existing_output(tmp_path: Path) -> None:
    fixture: Path = tmp_path / "fixture"
    fixture.mkdir()
    alias: Path = tmp_path / "fixture-link"
    alias.symlink_to(fixture, target_is_directory=True)
    with pytest.raises(ValueError, match="fixture"):
        _resolveScaleOutput(alias, configuredPath=str(fixture / "result.json"))
    output: Path = tmp_path / "owned-result.json"
    assert _resolveScaleOutput(alias, configuredPath=str(output)) == output
    output.write_text("immutable previous report")
    with pytest.raises(ValueError, match="exists"):
        _resolveScaleOutput(alias, configuredPath=str(output))
    assert output.read_text() == "immutable previous report"
