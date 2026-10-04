"""Resolve the server-owned corpus target without replacing caller identity."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from shared.models.database import document as private, demo_corpus as demo
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace

DEMO_NAMESPACE: str = "__knowhere_demo__"
DEMO_OWNER: str = "__knowhere_demo__"


@dataclass(frozen=True)
class CorpusStorage:
    target: Literal["PRIVATE", "DEMO"]

    @property
    def is_demo(self) -> bool:
        return self.target == "DEMO"

    def resolve_owner(self, caller_id: str) -> str:
        """Storage ownership only; auth, billing, telemetry keep the caller."""
        return DEMO_OWNER if self.is_demo else caller_id

    def compile_sql(self, statement: str) -> str:
        """Map fixed internal table identifiers in existing lexical SQL."""
        if not self.is_demo:
            return statement
        return re.sub(
            r"\b(" + "|".join(_TABLE_NAMES) + r")\b",
            lambda match: "demo_" + match.group(0),
            statement,
        )

    @property
    def Document(self) -> type[private.Document] | type[demo.DemoDocument]:
        return demo.DemoDocument if self.is_demo else private.Document

    @property
    def DocumentSection(
        self,
    ) -> type[private.DocumentSection] | type[demo.DemoDocumentSection]:
        return demo.DemoDocumentSection if self.is_demo else private.DocumentSection

    @property
    def DocumentChunk(
        self,
    ) -> type[private.DocumentChunk] | type[demo.DemoDocumentChunk]:
        return demo.DemoDocumentChunk if self.is_demo else private.DocumentChunk

    @property
    def DocumentMapUnit(
        self,
    ) -> type[private.DocumentMapUnit] | type[demo.DemoDocumentMapUnit]:
        return demo.DemoDocumentMapUnit if self.is_demo else private.DocumentMapUnit

    @property
    def DocumentMapUnitToken(
        self,
    ) -> type[private.DocumentMapUnitToken] | type[demo.DemoDocumentMapUnitToken]:
        return (
            demo.DemoDocumentMapUnitToken
            if self.is_demo
            else private.DocumentMapUnitToken
        )

    @property
    def DocumentMapUnitIndex(
        self,
    ) -> type[private.DocumentMapUnitIndex] | type[demo.DemoDocumentMapUnitIndex]:
        return (
            demo.DemoDocumentMapUnitIndex
            if self.is_demo
            else private.DocumentMapUnitIndex
        )

    @property
    def RetrievalNamespaceGeneration(
        self,
    ) -> (
        type[private.RetrievalNamespaceGeneration]
        | type[demo.DemoRetrievalNamespaceGeneration]
    ):
        return (
            demo.DemoRetrievalNamespaceGeneration
            if self.is_demo
            else private.RetrievalNamespaceGeneration
        )

    @property
    def RetrievalServingRevisionManifest(
        self,
    ) -> (
        type[private.RetrievalServingRevisionManifest]
        | type[demo.DemoRetrievalServingRevisionManifest]
    ):
        return (
            demo.DemoRetrievalServingRevisionManifest
            if self.is_demo
            else private.RetrievalServingRevisionManifest
        )

    @property
    def RetrievalNamespaceMapSnapshot(
        self,
    ) -> (
        type[private.RetrievalNamespaceMapSnapshot]
        | type[demo.DemoRetrievalNamespaceMapSnapshot]
    ):
        return (
            demo.DemoRetrievalNamespaceMapSnapshot
            if self.is_demo
            else private.RetrievalNamespaceMapSnapshot
        )

    @property
    def GraphNode(self) -> type[private.GraphNode] | type[demo.DemoGraphNode]:
        return demo.DemoGraphNode if self.is_demo else private.GraphNode

    @property
    def GraphEdge(self) -> type[private.GraphEdge] | type[demo.DemoGraphEdge]:
        return demo.DemoGraphEdge if self.is_demo else private.GraphEdge

    @property
    def RetrievalHitStat(
        self,
    ) -> type[private.RetrievalHitStat] | type[demo.DemoRetrievalHitStat]:
        return demo.DemoRetrievalHitStat if self.is_demo else private.RetrievalHitStat

    @classmethod
    def resolve_namespace(cls, namespace: str | None) -> CorpusStorage:
        return cls(
            "DEMO"
            if normalize_retrieval_namespace(namespace) == DEMO_NAMESPACE
            else "PRIVATE"
        )

    @classmethod
    def resolve_document(cls, document_id: str) -> CorpusStorage:
        return cls("DEMO" if document_id.startswith("ddoc_") else "PRIVATE")


_TABLE_NAMES: tuple[str, ...] = (
    "retrieval_serving_revision_manifests",
    "retrieval_namespace_map_snapshots",
    "retrieval_namespace_generations",
    "document_map_unit_indexes",
    "document_map_unit_tokens",
    "retrieval_hit_stats",
    "document_map_units",
    "document_sections",
    "document_chunks",
    "graph_nodes",
    "graph_edges",
    "documents",
)
