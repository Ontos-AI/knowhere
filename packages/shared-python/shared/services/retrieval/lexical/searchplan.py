"""Keep native query construction and RRF identical across both I/O runtimes."""

from pydantic import BaseModel, Field, ValidationError

from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.hit import LexicalHit
from shared.services.retrieval.lexical.unit import LexicalUnit

_CONTENT_COVERAGE_BONUS: float = 1.5
_MAXIMUM_COVERAGE_TERMS: int = 16
_MAXIMUM_TERMS_PER_FILTER: int = 65_536


class _StoredUnit(LexicalUnit):
    buildId: str


class _NativeHit(BaseModel):
    identity: str = Field(alias="_id")
    score: float = Field(alias="_score")
    source: _StoredUnit = Field(alias="_source")


class _Hits(BaseModel):
    hits: tuple[_NativeHit, ...]


class _Shards(BaseModel):
    failed: int


class _SearchResponse(BaseModel):
    hits: _Hits
    timed_out: bool
    shards: _Shards = Field(alias="_shards")


class NativeSearchPlan:
    @staticmethod
    def buildPayload(channel: str, *, buildIds: tuple[str, ...], queryTokens: tuple[str, ...],
                     limit: int, unitIds: tuple[str, ...] | None,
                     preferContentCoverage: bool = False,
                     sectionIds: tuple[str, ...] | None = None) -> dict[str, object]:
        filters: list[dict[str, object]] = [{"terms": {"buildId": list(buildIds)}}]
        if unitIds is not None:
            filters.append({"terms": {"unitId": list(unitIds)}})
        if sectionIds is not None:
            if len(sectionIds) <= _MAXIMUM_TERMS_PER_FILTER:
                filters.append({"terms": {"sectionId": list(sectionIds)}})
            else:
                filters.append({"bool": {"minimum_should_match": 1, "should": [
                    {"terms": {"sectionId": list(sectionIds[offset:offset + _MAXIMUM_TERMS_PER_FILTER])}}
                    for offset in range(0, len(sectionIds), _MAXIMUM_TERMS_PER_FILTER)
                ]}})
        query: dict[str, object] = {
            "filter": filters,
            "must": [{"match": {channel: {"query": " ".join(queryTokens), "operator": "or"}}}],
        }
        if preferContentCoverage and channel == "contentTokens":
            terms: tuple[str, ...] = tuple(dict.fromkeys(queryTokens))
            if 2 <= len(terms) <= _MAXIMUM_COVERAGE_TERMS:
                # Each distinct match contributes once, independent of repetition.
                query["should"] = [
                    {"constant_score": {"filter": {"term": {channel: term}}, "boost": _CONTENT_COVERAGE_BONUS}}
                    for term in terms
                ]
        return {"size": max(100, limit), "_source": {"excludes": ["pathTokens", "contentTokens"]},
            "query": {"bool": query},
            "sort": [{"_score": "desc"}, {"documentId": "asc"}, {"sortOrder": "asc"}, {"unitId": "asc"}]}

    @staticmethod
    def fuseChannels(bodies: tuple[object, object], *, buildIds: tuple[str, ...],
                     limit: int) -> tuple[LexicalHit, ...]:
        try:
            channels: tuple[_SearchResponse, ...] = tuple(_SearchResponse.model_validate(body) for body in bodies)
        except ValidationError as error:
            raise LexicalIndexError("Native lexical response was invalid") from error
        if any(channel.timed_out or channel.shards.failed for channel in channels):
            raise LexicalIndexError("Lexical channel returned partial results", isRetryable=True)
        hits: dict[str, LexicalHit] = {}
        for channelNumber, channel in enumerate(channels):
            for rank, native in enumerate(channel.hits.hits):
                if native.source.buildId not in buildIds:
                    raise LexicalIndexError(
                        "Native hit escaped its published build scope"
                    )
                previous: LexicalHit | None = hits.get(native.identity)
                hits[native.identity] = LexicalHit(
                    unit=LexicalUnit.model_validate(
                        native.source.model_dump(exclude={"buildId"})
                    ),
                    buildId=native.source.buildId,
                    score=(previous.score if previous else 0)
                    + (1 if channelNumber == 0 else 2) / (60 + rank + 1),
                    pathRank=rank
                    if channelNumber == 0
                    else previous.pathRank
                    if previous
                    else None,
                    contentRank=rank
                    if channelNumber == 1
                    else previous.contentRank
                    if previous
                    else None,
                )
        return tuple(
            sorted(
                hits.values(),
                key=lambda hit: (
                    -hit.score,
                    hit.unit.documentId,
                    hit.unit.sortOrder,
                    hit.unit.unitId,
                ),
            )[:limit]
        )
