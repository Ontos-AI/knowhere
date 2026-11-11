"""Filters applied to the same immutable native records as manifest scoring."""

from shared.services.retrieval.document_scope import DocumentScope


def buildManifestFilters(
    *, scope: DocumentScope, chunkTypes: set[str] | None, excludeSections: list[dict[str, str]],
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
