"""Canonical Demo Source catalog and file access."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.services.demo.official_library_catalog import (
    get_official_library_catalog,
    get_official_library_source_payload_by_demo_source_id,
)
from app.services.demo.source_projection import DemoSourceProjection


@dataclass(frozen=True)
class DemoCitationDefinition:
    section_path: str
    description: str
    content: str


@dataclass(frozen=True)
class DemoExampleDefinition:
    id: str
    question: str
    answer: str
    citations: tuple[DemoCitationDefinition, ...]


@dataclass(frozen=True)
class DemoSourceDefinition:
    demo_source_id: str
    canonical_document_id: str
    title: str
    mime_type: str
    size_bytes: int
    asset_directory: str
    chunk_count: int
    examples: tuple[DemoExampleDefinition, ...]
    original_file_name: str | None = "original.pdf"


_DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "demo_documents"
_ASSET_DIRECTORY_NAMES = frozenset({"images", "tables", "page_citation_assets"})
_DEMO_SOURCE_DEFINITIONS: tuple[DemoSourceDefinition, ...] = (
    DemoSourceDefinition(
        demo_source_id="demo-tsla-q4-2025",
        canonical_document_id="demo-doc-tsla-q4-2025",
        title="TSLA-Q4-2025-Update.pdf",
        mime_type="application/pdf",
        size_bytes=5_648_867,
        asset_directory="tsla-q4-2025",
        chunk_count=71,
        examples=(
            DemoExampleDefinition(
                id="demo-tsla-q4-2025-xai",
                question="What does the document say about Tesla's xAI investment?",
                answer=(
                    "Tesla entered an agreement on January 16, 2026 to invest "
                    "approximately $2 billion in xAI Series E Preferred Stock.\n\n"
                    "The document also says Tesla and xAI entered a framework "
                    "agreement to evaluate AI collaboration, with the investment "
                    "expected to close in Q1 2026 subject to customary regulatory "
                    "conditions."
                ),
                citations=(
                    DemoCitationDefinition(
                        section_path=("TSLA-Q4-2025-Update.pdf/OTHER UPDATES"),
                        description="xAI investment",
                        content=(
                            "On January 16, 2026, Tesla entered into an agreement "
                            "to invest approximately"
                        ),
                    ),
                ),
            ),
            DemoExampleDefinition(
                id="demo-tsla-q4-2025-energy-storage",
                question="What does the document say about energy storage?",
                answer=(
                    "Tesla achieved its highest quarterly energy storage "
                    "deployments, driven by record Megapack deployments.\n\n"
                    "Energy gross profit reached a record $1.1 billion, marking "
                    "the fifth consecutive record quarter.\n\n"
                    "Tesla also plans to begin Megapack 3 and Megablock "
                    "production at Megafactory Houston in 2026."
                ),
                citations=(
                    DemoCitationDefinition(
                        section_path=(
                            "TSLA-Q4-2025-Update.pdf/"
                            "MANUFACTURING & HARDWARE/"
                            "Energy generation and storage"
                        ),
                        description="Storage deployment growth",
                        content=(
                            "record quarterly energy storage deployments "
                            "driven by Megapack"
                        ),
                    ),
                ),
            ),
            DemoExampleDefinition(
                id="demo-tsla-q4-2025-production-plans",
                question="What production plans does Tesla mention for 2026?",
                answer=(
                    "Tesla says Cybercab, Tesla Semi, and Megapack 3 are on "
                    "schedule for volume production starting in 2026.\n\n"
                    "The same product update also notes that first-generation "
                    "Optimus production lines are being installed before volume "
                    "production."
                ),
                citations=(
                    DemoCitationDefinition(
                        section_path=("TSLA-Q4-2025-Update.pdf/OUTLOOK/Volume"),
                        description="2026 production plans",
                        content=(
                            "Cybercab, Tesla Semi and Megapack 3 are on schedule "
                            "for volume production starting in 2026."
                        ),
                    ),
                ),
            ),
        ),
    ),
    DemoSourceDefinition(
        demo_source_id="demo-spacex-s1",
        canonical_document_id="demo-doc-spacex-s1",
        title="spacex-s1.pdf",
        mime_type="application/pdf",
        size_bytes=7_441_414,
        asset_directory="spacex-s1",
        chunk_count=227,
        examples=(
            DemoExampleDefinition(
                id="demo-spacex-s1-starlink-scale",
                question="What does the filing say about Starlink's scale?",
                answer=(
                    "The filing says SpaceX operates a high-speed, low-latency "
                    "global broadband network powered by about 9,600 Starlink "
                    "broadband and mobile satellites in Low-Earth Orbit.\n\n"
                    "It says that network serves consumer, enterprise, and "
                    "government customers across 164 countries, territories, "
                    "and other markets as of March 31, 2026."
                ),
                citations=(
                    DemoCitationDefinition(
                        section_path="spacex-s1.pdf/Root",
                        description="Starlink network scale",
                        content=(
                            "approximately 9,600 Starlink broadband "
                            "and mobile satellites"
                        ),
                    ),
                ),
            ),
            DemoExampleDefinition(
                id="demo-spacex-s1-launch-reusability",
                question="How does the filing describe SpaceX's launch reusability?",
                answer=(
                    "The filing says Falcon 9 reusability gave SpaceX a "
                    "step-function cost advantage in space access.\n\n"
                    "It also says Falcon 9 first stages had demonstrated the "
                    "ability to refly 34 times as of March 31, 2026."
                ),
                citations=(
                    DemoCitationDefinition(
                        section_path="spacex-s1.pdf/Root",
                        description="Falcon 9 booster reuse",
                        content=(
                            "refly a first-stage 34 times"
                        ),
                    ),
                ),
            ),
            DemoExampleDefinition(
                id="demo-spacex-s1-offering",
                question="What offering does the filing describe?",
                answer=(
                    "The filing describes an initial public offering of shares "
                    "of Space Exploration Technologies Corp. Class A common stock."
                ),
                citations=(
                    DemoCitationDefinition(
                        section_path="spacex-s1.pdf/Root",
                        description="Class A common stock offering",
                        content=(
                            "This is the initial public offering of shares of "
                            "Class A common stock"
                        ),
                    ),
                ),
            ),
        ),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-nvda-q1-fy27-earnings-call",
        canonical_document_id="demo-doc-financial-nvda-q1-fy27-earnings-call",
        title="NVDA Q1 FY27 Earnings Call Transcript.pdf",
        mime_type="application/pdf",
        size_bytes=288_661,
        asset_directory="financial-nvda-q1-fy27-earnings-call",
        chunk_count=17,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-nvda-q4-fy26-earnings-call",
        canonical_document_id="demo-doc-financial-nvda-q4-fy26-earnings-call",
        title="NVDA Q4 FY26 Earnings Call Transcript.pdf",
        mime_type="application/pdf",
        size_bytes=306_317,
        asset_directory="financial-nvda-q4-fy26-earnings-call",
        chunk_count=24,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-nvda-q1-fy27-cfo-commentary",
        canonical_document_id="demo-doc-financial-nvda-q1-fy27-cfo-commentary",
        title="NVIDIA Q1 FY27 CFO Commentary.pdf",
        mime_type="application/pdf",
        size_bytes=75_796,
        asset_directory="financial-nvda-q1-fy27-cfo-commentary",
        chunk_count=11,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-nvda-q1-fy27-results",
        canonical_document_id="demo-doc-financial-nvda-q1-fy27-results",
        title="NVIDIA Q1 FY27 Results.pdf",
        mime_type="application/pdf",
        size_bytes=379_572,
        asset_directory="financial-nvda-q1-fy27-results",
        chunk_count=98,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-nvda-q1-fy27-presentation",
        canonical_document_id="demo-doc-financial-nvda-q1-fy27-presentation",
        title="NVIDIA Q1 FY27 Quarterly Presentation.pdf",
        mime_type="application/pdf",
        size_bytes=3_371_774,
        asset_directory="financial-nvda-q1-fy27-presentation",
        chunk_count=28,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-nvda-fy26-annual-report",
        canonical_document_id="demo-doc-financial-nvda-fy26-annual-report",
        title="NVIDIA FY26 Annual Report.pdf",
        mime_type="application/pdf",
        size_bytes=15_850_437,
        asset_directory="financial-nvda-fy26-annual-report",
        chunk_count=368,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-micron-report-530bd7ed",
        canonical_document_id="demo-doc-financial-micron-report-530bd7ed",
        title="Micron investor report 530bd7ed.pdf",
        mime_type="application/pdf",
        size_bytes=4_497_791,
        asset_directory="financial-micron-report-530bd7ed",
        chunk_count=77,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-micron-report-9c0becf5",
        canonical_document_id="demo-doc-financial-micron-report-9c0becf5",
        title="Micron investor report 9c0becf5.pdf",
        mime_type="application/pdf",
        size_bytes=4_383_420,
        asset_directory="financial-micron-report-9c0becf5",
        chunk_count=82,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-goog-10-k-2025",
        canonical_document_id="demo-doc-financial-goog-10-k-2025",
        title="GOOG 10-K 2025.pdf",
        mime_type="application/pdf",
        size_bytes=940_068,
        asset_directory="financial-goog-10-k-2025",
        chunk_count=1,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-meta-q1-2026-results",
        canonical_document_id="demo-doc-financial-meta-q1-2026-results",
        title="Meta Q1 2026 Exhibit 99.1.pdf",
        mime_type="application/pdf",
        size_bytes=154_270,
        asset_directory="financial-meta-q1-2026-results",
        chunk_count=17,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-meta-q1-2026-presentation",
        canonical_document_id="demo-doc-financial-meta-q1-2026-presentation",
        title="Meta Q1 2026 Earnings Presentation.pdf",
        mime_type="application/pdf",
        size_bytes=239_814,
        asset_directory="financial-meta-q1-2026-presentation",
        chunk_count=32,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-meta-q1-2026-earnings-call",
        canonical_document_id="demo-doc-financial-meta-q1-2026-earnings-call",
        title="Meta Q1 2026 Earnings Call Transcript.pdf",
        mime_type="application/pdf",
        size_bytes=131_758,
        asset_directory="financial-meta-q1-2026-earnings-call",
        chunk_count=3,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-microsoft-2025-annual-report",
        canonical_document_id="demo-doc-financial-microsoft-2025-annual-report",
        title="Microsoft 2025 Annual Report.docx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        size_bytes=1_044_155,
        asset_directory="financial-microsoft-2025-annual-report",
        chunk_count=85,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-financial-alibaba-fy2026-interim-report",
        canonical_document_id="demo-doc-financial-alibaba-fy2026-interim-report",
        title="Alibaba Fiscal Year 2026 Interim Report.pdf",
        mime_type="application/pdf",
        size_bytes=346_418,
        asset_directory="financial-alibaba-fy2026-interim-report",
        chunk_count=125,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-research-attention-is-all-you-need",
        canonical_document_id="demo-doc-research-attention-is-all-you-need",
        title="Attention Is All You Need.pdf",
        mime_type="application/pdf",
        size_bytes=2_215_244,
        asset_directory="research-attention-is-all-you-need",
        chunk_count=34,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-research-rag-survey",
        canonical_document_id="demo-doc-research-rag-survey",
        title="Retrieval-Augmented Generation for Large Language Models: A Survey.pdf",
        mime_type="application/pdf",
        size_bytes=1_662_567,
        asset_directory="research-rag-survey",
        chunk_count=35,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-research-rag-realized",
        canonical_document_id="demo-doc-research-rag-realized",
        title="Retrieval-Augmented Generation Realized.pdf",
        mime_type="application/pdf",
        size_bytes=3_979_559,
        asset_directory="research-rag-realized",
        chunk_count=59,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-research-toolformer",
        canonical_document_id="demo-doc-research-toolformer",
        title="Toolformer: Language Models Can Teach Themselves to Use Tools.pdf",
        mime_type="application/pdf",
        size_bytes=657_966,
        asset_directory="research-toolformer",
        chunk_count=41,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-research-ai-agents-overview",
        canonical_document_id="demo-doc-research-ai-agents-overview",
        title="AI Agents Overview.pdf",
        mime_type="application/pdf",
        size_bytes=4_928_076,
        asset_directory="research-ai-agents-overview",
        chunk_count=58,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-stem-jurafsky-transformers",
        canonical_document_id="demo-doc-stem-jurafsky-transformers",
        title="Speech and Language Processing, Chapter 8: Transformers.pdf",
        mime_type="application/pdf",
        size_bytes=1_363_617,
        asset_directory="stem-jurafsky-transformers",
        chunk_count=47,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-stem-transformers-tutorial",
        canonical_document_id="demo-doc-stem-transformers-tutorial",
        title="Transformers Tutorial.pdf",
        mime_type="application/pdf",
        size_bytes=12_824_695,
        asset_directory="stem-transformers-tutorial",
        chunk_count=301,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-stem-deep-learning-transformer-network",
        canonical_document_id="demo-doc-stem-deep-learning-transformer-network",
        title="Deep Learning - Transformer Network.pdf",
        mime_type="application/pdf",
        size_bytes=2_619_505,
        asset_directory="stem-deep-learning-transformer-network",
        chunk_count=18,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-stem-probability-theory-lecture-notes",
        canonical_document_id="demo-doc-stem-probability-theory-lecture-notes",
        title="Probability Theory Lecture Notes.pdf",
        mime_type="application/pdf",
        size_bytes=708_671,
        asset_directory="stem-probability-theory-lecture-notes",
        chunk_count=68,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-stem-introduction-statistical-learning-theory",
        canonical_document_id="demo-doc-stem-introduction-statistical-learning-theory",
        title="Introduction to Statistical Learning Theory.pdf",
        mime_type="application/pdf",
        size_bytes=1_208_674,
        asset_directory="stem-introduction-statistical-learning-theory",
        chunk_count=49,
        examples=(),
        original_file_name=None,
    ),
    DemoSourceDefinition(
        demo_source_id="demo-stem-statistical-learning",
        canonical_document_id="demo-doc-stem-statistical-learning",
        title="Statistical Learning Notes.pdf",
        mime_type="application/pdf",
        size_bytes=614_119,
        asset_directory="stem-statistical-learning",
        chunk_count=45,
        examples=(),
        original_file_name=None,
    ),
)


class DemoSourceCatalog:
    def __init__(self, *, projection: DemoSourceProjection | None = None) -> None:
        self._projection = projection or DemoSourceProjection()

    def list_sources(self) -> tuple[DemoSourceDefinition, ...]:
        return _DEMO_SOURCE_DEFINITIONS

    def get_catalog(self) -> dict[str, Any]:
        source_payloads = [
            self._projection.source_catalog_payload(
                source=source,
                chunks=_load_source_chunks(source) if source.examples else (),
            )
            for source in _DEMO_SOURCE_DEFINITIONS
        ]
        library_source_payload_by_demo_source_id = (
            get_official_library_source_payload_by_demo_source_id()
        )
        for payload in source_payloads:
            demo_source_id = str(payload["demo_source_id"])
            library_payload = library_source_payload_by_demo_source_id.get(
                demo_source_id
            )
            if library_payload is not None:
                payload["official_library"] = library_payload

        return {
            "sources": source_payloads,
            "official_library": get_official_library_catalog(
                {str(payload["demo_source_id"]): payload for payload in source_payloads}
            ),
        }

    def list_chunks(
        self,
        *,
        demo_source_id: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any] | None:
        source = self.get_source(demo_source_id)
        if source is None:
            return None

        chunks = _load_source_chunks(source)
        start = (page - 1) * page_size
        page_chunks = chunks[start : start + page_size]
        return {
            "demo_source_id": source.demo_source_id,
            "canonical_document_id": source.canonical_document_id,
            "title": source.title,
            "mime_type": source.mime_type,
            "chunks": [
                self._projection.chunk_payload(
                    source=source,
                    chunk=chunk,
                    sort_order=start + index,
                )
                for index, chunk in enumerate(page_chunks)
            ],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": len(chunks),
                "total_pages": math.ceil(len(chunks) / page_size) if chunks else 0,
            },
        }

    def get_chunk(
        self,
        *,
        demo_source_id: str,
        demo_chunk_id: str,
    ) -> dict[str, Any] | None:
        source = self.get_source(demo_source_id)
        if source is None:
            return None

        chunks = _load_source_chunks(source)
        for sort_order, chunk in enumerate(chunks):
            if self._projection.matches_chunk_id(
                source=source,
                chunk=chunk,
                demo_chunk_id=demo_chunk_id,
            ):
                return {
                    "demo_source_id": source.demo_source_id,
                    "canonical_document_id": source.canonical_document_id,
                    "chunk": self._projection.chunk_payload(
                        source=source,
                        chunk=chunk,
                        sort_order=sort_order,
                    ),
                }

        return None

    def get_original_file_path(self, *, demo_source_id: str) -> Path | None:
        source = self.get_source(demo_source_id)
        if source is None:
            return None
        if source.original_file_name is None:
            return None

        file_path = self.source_directory(source) / source.original_file_name
        return file_path if file_path.is_file() else None

    def get_asset_file_path(
        self,
        *,
        demo_source_id: str,
        asset_path: str,
    ) -> Path | None:
        source = self.get_source(demo_source_id)
        if source is None:
            return None

        source_directory = self.source_directory(source).resolve()
        normalized_asset_path = _normalize_asset_path(asset_path)
        if normalized_asset_path is None:
            return None

        candidate = (source_directory / normalized_asset_path).resolve()
        if not candidate.is_relative_to(source_directory):
            return None

        return candidate if candidate.is_file() else None

    def require_source(self, demo_source_id: str) -> DemoSourceDefinition:
        source = self.get_source(demo_source_id)
        if source is None:
            raise KeyError(demo_source_id)
        return source

    def get_source(self, demo_source_id: str) -> DemoSourceDefinition | None:
        return next(
            (
                source
                for source in _DEMO_SOURCE_DEFINITIONS
                if source.demo_source_id == demo_source_id
            ),
            None,
        )

    def source_directory(self, source: DemoSourceDefinition) -> Path:
        return _DATA_ROOT / source.asset_directory

    def publication_chunks(self, source: DemoSourceDefinition) -> list[dict[str, Any]]:
        return self._projection.publication_chunks(
            source=source,
            chunks=_load_source_chunks(source),
        )


def _normalize_asset_path(asset_path: str) -> Path | None:
    normalized = str(asset_path or "").strip().replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or parts[0] not in _ASSET_DIRECTORY_NAMES:
        return None
    if any(part == ".." or part.startswith(".") for part in parts):
        return None
    return Path(*parts)


@lru_cache(maxsize=8)
def _load_source_chunks(source: DemoSourceDefinition) -> tuple[dict[str, Any], ...]:
    chunks_path = (_DATA_ROOT / source.asset_directory) / "chunks.json"
    with chunks_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    chunks = payload.get("chunks") if isinstance(payload, dict) else None
    if not isinstance(chunks, list):
        return ()

    return tuple(
        dict(chunk)
        for chunk in chunks
        if isinstance(chunk, dict) and isinstance(chunk.get("chunk_id"), str)
    )
