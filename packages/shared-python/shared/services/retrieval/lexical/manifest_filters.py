"""Filters applied to the same immutable native records as manifest scoring."""

from shared.services.retrieval.document_scope import DocumentScope


def _wildcardLiteral(value: str) -> str:
    return value.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")


def _sqlLikePattern(value: str) -> str:
    # Match the existing PostgreSQL substring filter, including LIKE wildcards.
    pattern: list[str] = []
    escaped: bool = False
    for char in "%" + value + "%":
        if escaped:
            pattern.append(_wildcardLiteral(char))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "%":
            pattern.append("*")
        elif char == "_":
            pattern.append("?")
        else:
            pattern.append(_wildcardLiteral(char))
    return "".join(pattern)


def buildManifestFilters(
    *, scope: DocumentScope, chunkTypes: set[str] | None, signalPaths: list[str],
    filterMode: str, excludeSections: list[dict[str, str]],
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    filters: list[dict[str, object]] = []
    exclusions: list[dict[str, object]] = []
    if scope.include is not None:
        filters.append({"terms": {"documentId": sorted(scope.include)}})
    if scope.exclude:
        exclusions.append({"terms": {"documentId": sorted(scope.exclude)}})
    if chunkTypes is not None and chunkTypes.issubset({"image", "table"}):
        assetFields: dict[str, str] = {"image": "hasImage", "table": "hasTable"}
        assets = [{"term": {assetFields[chunkType]: True}} for chunkType in sorted(chunkTypes)]
        filters.append({"bool": {"should": assets, "minimum_should_match": 1}} if assets else {"match_none": {}})
    if signalPaths:
        signals = [{"wildcard": {"sectionPath": {"value": _sqlLikePattern(path.lower()), "case_insensitive": True}}}
                   for path in signalPaths]
        signal: dict[str, object] = {"bool": {"should": signals, "minimum_should_match": 1}}
        (filters if filterMode == "keep" else exclusions).append(signal)
    for section in excludeSections:
        documentId = str(section.get("document_id") or "").strip()
        path = str(section.get("section_path") or "").strip()
        if documentId and path:
            exclusions.append({"bool": {"filter": [
                {"term": {"documentId": documentId}},
                {"bool": {"minimum_should_match": 1, "should": [
                    {"term": {"sectionPath": path}},
                    {"prefix": {"sectionPath": path + " / "}},
                ]}},
            ]}})
    return tuple(filters), tuple(exclusions)
