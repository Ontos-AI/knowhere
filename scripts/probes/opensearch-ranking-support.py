"""Versioned replay inputs and evidence-group replacement gates.

This module is deliberately independent of database/application imports so the
acceptance evaluator can be tested without starting PostgreSQL or OpenSearch.
Only ReplaySupport is public. Derived filters never claim human relevance review.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__: list[str] = ["ReplaySupport"]


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    chunks_path: Path
    replacement_chunks_path: Path | None = None
    replacement_file_name: str | None = None
    replace: bool = False


class _Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    document: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)


class _ExcludedSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    document: str = Field(min_length=1)
    section_path: str = Field(min_length=1)


class _Query(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    text: str = Field(min_length=1)
    top_k: int = Field(ge=1)
    expected: list[list[_Evidence]]
    replacement_expected: list[list[_Evidence]] | None = None
    include_documents: list[str] | None = None
    exclude_documents: list[str] = Field(default_factory=list)
    exclude_sections: list[_ExcludedSection] = Field(default_factory=list)
    chunk_types: list[Literal["text", "page", "image", "table"]] | None = None
    internal_recall_k: int | None = Field(default=None, ge=1)
    require_empty: bool = False
    derived_from_query: str | None = None


class _Suite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["baseline", "extended"] = "baseline"
    id: str = Field(default="original-20", min_length=1)
    version: str = Field(default="1", min_length=1)
    baseline_manifest_sha256: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    suite: _Suite = Field(default_factory=_Suite)
    provenance: Literal["public_preview", "business_reviewed", "automated_derived"]
    reviewer: str | None = Field(default=None, min_length=1)
    derivation: dict[str, str | bool] | None = None
    documents: list[_Document] = Field(min_length=1)
    queries: list[_Query] = Field(min_length=1)

    @model_validator(mode="after")
    def validateContract(self) -> Self:
        if self.suite.kind == "baseline":
            if len(self.queries) != 20:
                raise ValueError("The immutable baseline requires exactly 20 queries")
            if len({(query.namespace, query.text) for query in self.queries}) != 20:
                raise ValueError("The baseline requires 20 unique namespace/text pairs")
        elif self.suite.baseline_manifest_sha256 is None:
            raise ValueError("An extended suite must identify its baseline manifest digest")
        if len({query.id for query in self.queries}) != len(self.queries):
            raise ValueError("A suite requires unique query IDs")
        documents: dict[str, _Document] = {doc.key: doc for doc in self.documents}
        if len(documents) != len(self.documents):
            raise ValueError("A suite requires unique document keys")
        if self.provenance == "automated_derived":
            if self.reviewer is not None or not self.derivation:
                raise ValueError("Automated derived cases require derivation, not a human reviewer")
            if self.suite.kind != "extended":
                raise ValueError("Automated derived cases cannot replace the baseline")
        elif self.reviewer is None:
            raise ValueError("Reviewed inputs require a named reviewer")
        for query in self.queries:
            self.validateQuery(query, documents)
        return self

    def validateQuery(self, query: _Query, documents: dict[str, _Document]) -> None:
        if not query.text.strip():
            raise ValueError("A query must contain non-whitespace text")
        if self.provenance != "automated_derived" and not query.expected:
            raise ValueError("Do not drop reviewed evidence groups")
        for groups in (query.expected, query.replacement_expected):
            if groups is None:
                continue
            if self.provenance != "automated_derived" and not groups:
                raise ValueError("Do not drop reviewed evidence groups")
            for group in groups:
                if not group:
                    raise ValueError("Each evidence group must retain at least one identity")
                for evidence in group:
                    if evidence.document not in documents:
                        raise ValueError("Evidence refers to an unknown document")
                    if documents[evidence.document].namespace != query.namespace:
                        raise ValueError("Evidence must belong to the frozen query namespace")
        filterKeys: set[str] = set(query.include_documents or []) | set(query.exclude_documents)
        filterKeys.update(section.document for section in query.exclude_sections)
        if not filterKeys <= documents.keys():
            raise ValueError("A filter refers to an unknown document")


class _Comparison(BaseModel):
    id: str = Field(min_length=1)
    top_k: int = Field(ge=1)
    evidence_groups: list[list[_Evidence]]
    ranks: dict[Literal["postgresql", "opensearch"], list[int | None]]
    identity_failures: list[str]

    @model_validator(mode="after")
    def validateObservations(self) -> Self:
        if set(self.ranks) != {"postgresql", "opensearch"}:
            raise ValueError("Every query requires both comparable backends")
        for ranks in self.ranks.values():
            if len(ranks) != len(self.evidence_groups):
                raise ValueError("Each evidence group requires one rank observation per backend")
            if any(rank is not None and not 1 <= rank <= self.top_k for rank in ranks):
                raise ValueError("Observed evidence ranks must fit the frozen final result count")
        return self


class _Stage(BaseModel):
    stage: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    queries: list[_Comparison] = Field(min_length=1)


class _Report(BaseModel):
    stages: list[_Stage] = Field(min_length=1)


class ReplaySupport:
    """Validate frozen suites and evaluate all stage/query/evidence boundaries."""

    @staticmethod
    def parseManifest(value: object) -> _Manifest:
        return _Manifest.model_validate(value)

    @staticmethod
    def loadManifest(path: Path) -> _Manifest:
        return _Manifest.model_validate_json(path.read_bytes())

    @staticmethod
    def findIdentityFailures(
        actual: dict[str, object], canonical: dict[str, object]
    ) -> list[str]:
        return [
            f"{field}: differs from canonical published data"
            for field, expected in canonical.items()
            if field not in actual or actual[field] != expected
        ]

    @staticmethod
    def evaluateReport(value: object) -> dict[str, object]:
        report: _Report = _Report.model_validate(value)
        counts: dict[str, int] = {
            "shared_success": 0,
            "postgresql_only": 0,
            "opensearch_only": 0,
            "shared_miss": 0,
        }
        groups: list[dict[str, object]] = []
        failures: list[dict[str, str]] = []
        boundaries: set[tuple[str, str, str]] = set()
        for stage in report.stages:
            for query in stage.queries:
                boundary: tuple[str, str, str] = (stage.stage, stage.namespace, query.id)
                if boundary in boundaries:
                    raise ValueError("Duplicate stage/namespace/query comparison")
                boundaries.add(boundary)
                for reason in query.identity_failures:
                    failures.append({
                        "stage": stage.stage,
                        "namespace": stage.namespace,
                        "query_id": query.id,
                        "reason": reason,
                    })
                for index, evidence in enumerate(query.evidence_groups):
                    postgresqlRank: int | None = query.ranks["postgresql"][index]
                    opensearchRank: int | None = query.ranks["opensearch"][index]
                    status: str = (
                        "shared_success" if opensearchRank is not None else "postgresql_only"
                    ) if postgresqlRank is not None else (
                        "opensearch_only" if opensearchRank is not None else "shared_miss"
                    )
                    counts[status] += 1
                    groups.append({
                        "stage": stage.stage,
                        "namespace": stage.namespace,
                        "query_id": query.id,
                        "group_index": index,
                        "evidence": [item.model_dump() for item in evidence],
                        "postgresql_rank": postgresqlRank,
                        "opensearch_rank": opensearchRank,
                        "status": status,
                    })
        return {
            "evaluator_version": "relative-evidence-groups-v1",
            "passed": counts["postgresql_only"] == 0 and not failures,
            "query_stage_comparisons": len(boundaries),
            "counts": counts,
            "groups": groups,
            "identity_failures": failures,
        }

    @staticmethod
    def deriveExtendedManifest(
        baselinePath: Path, *, suiteId: str, suiteVersion: str
    ) -> _Manifest:
        baseline: _Manifest = ReplaySupport.loadManifest(baselinePath)
        if baseline.suite.kind != "baseline":
            raise ValueError("Filter derivation requires the unchanged baseline manifest")
        documents: list[dict[str, object]] = []
        for document in baseline.documents:
            record: dict[str, object] = document.model_dump(mode="json")
            record["chunks_path"] = str((baselinePath.parent / document.chunks_path).resolve())
            if document.replacement_chunks_path is not None:
                record["replacement_chunks_path"] = str(
                    (baselinePath.parent / document.replacement_chunks_path).resolve()
                )
            documents.append(record)
        queries: list[dict[str, object]] = []
        for query in baseline.queries:
            original: dict[str, object] = query.model_dump(mode="json")
            marked: list[str] = sorted({
                evidence.document
                for group in [*query.expected, *(query.replacement_expected or [])]
                for evidence in group
            })
            queries.append({
                **original, "id": query.id + "--include-marked",
                "include_documents": marked, "derived_from_query": query.id,
            })
            queries.append({
                **original, "id": query.id + "--exclude-all",
                "expected": [], "replacement_expected": [],
                "exclude_documents": [doc.key for doc in baseline.documents],
                "require_empty": True, "derived_from_query": query.id,
            })
            foreign: list[str] = [
                doc.key for doc in baseline.documents if doc.namespace != query.namespace
            ]
            if foreign:
                queries.append({
                    **original, "id": query.id + "--cross-namespace",
                    "expected": [], "replacement_expected": [],
                    "include_documents": foreign, "require_empty": True,
                    "derived_from_query": query.id,
                })
        return _Manifest.model_validate({
            "suite": {
                "kind": "extended", "id": suiteId, "version": suiteVersion,
                "baseline_manifest_sha256": sha256(baselinePath.read_bytes()).hexdigest(),
            },
            "provenance": "automated_derived",
            "reviewer": None,
            "derivation": {
                "method": "baseline-filter-variants-v1",
                "human_relevance_reviewed": False,
                "limitation": "Structural/filter checks reuse baseline labels; broader human relevance review remains required.",
            },
            "documents": documents,
            "queries": queries,
        })
