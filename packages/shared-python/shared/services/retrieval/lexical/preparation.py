"""One authoritative map-unit compiler with cooperative API preparation."""

from collections.abc import Awaitable, Callable, Iterator, Sequence
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
from itertools import chain
from pathlib import Path

from shared.core.config import settings
from shared.services.retrieval.lexical.compilation_checkpoint import CompilationCheckpoint
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.input import LexicalInput
from shared.services.retrieval.lexical.unit import LexicalUnit
from shared.services.retrieval.scoring.hierarchy import ProviderToolSpace
from shared.services.retrieval.scoring.knowhere_hybrid import (
    _iterate_content_search_text, _iterate_path_search_text, build_content_search_text,
)
from shared.services.retrieval.scoring.knowhere_provider import KnowhereProvider, SectionRow, UnitRow
from shared.services.retrieval.scoring.score_units import _iterate_score_units


@lru_cache(maxsize=1)
def _getCompilerFingerprint() -> str:
    import jieba
    from shared.services.retrieval.scoring import score_units, knowhere_hybrid, knowhere_provider, hierarchy
    from shared.services.retrieval.search import lexical_text
    from shared.services.retrieval import publication_preparation_cache
    from shared.utils import text_utils

    digest = sha256()
    for module in (score_units, knowhere_hybrid, lexical_text, knowhere_provider,
                   hierarchy, publication_preparation_cache, text_utils):
        digest.update(Path(str(module.__file__)).read_bytes())
    digest.update(Path(__file__).read_bytes())
    digest.update((Path(str(jieba.__file__)).parent / "dict.txt").read_bytes())
    digest.update(f"jieba:{version('jieba')};syntok:{version('syntok')}".encode())
    # Identical installations can select a different English backend when the
    # optional native library cannot load on a platform. Pin the effective mode,
    # not merely the installed Python package metadata.
    englishTokenizer: str = (f"blingfire:{version('blingfire')}"
        if text_utils._blingfire_text_to_words is not None else "syntok")
    digest.update(f"english-tokenizer:{englishTokenizer}".encode())
    return digest.hexdigest()


def _checkSize(size: int, limit: int) -> None:
    if size > limit:
        raise LexicalIndexError(f"Indexing raw record or scoring unit exceeds its character/work limit ({size} > {limit})", isSizeLimit=True)


def _validateMetadata(metadata: object, limit: int) -> Iterator[None]:
    # Count string characters plus one work item per JSON node. Stream nested
    # containers rather than materializing their children before yielding.
    frames: list[tuple[Iterator[object], int | None]] = [(iter((metadata,)), None)]
    active: set[int] = set()
    work: int = 0
    sentinel: object = object()
    while frames:
        iterator, owner = frames[-1]
        value: object = next(iterator, sentinel)
        if value is sentinel:
            frames.pop()
            if owner is not None:
                active.remove(owner)
            continue
        work += len(value) + 1 if isinstance(value, str) else 1
        _checkSize(work, limit)
        if isinstance(value, (dict, list, tuple)):
            identity: int = id(value)
            if identity in active:
                raise LexicalIndexError("Indexing metadata contains a container cycle")
            active.add(identity)
            children: Iterator[object] = chain(value.keys(), value.values()) if isinstance(value, dict) else iter(value)
            frames.append((children, identity))
        yield None


def _validateRows(sections: Sequence[SectionRow], chunks: Sequence[UnitRow], limit: int) -> Iterator[None]:
    parents: dict[str, SectionRow] = {}
    for section in sections:
        if section.section_id in parents:
            raise LexicalIndexError("Indexing hierarchy contains duplicate section identities")
        parents[section.section_id] = section
        _checkSize(len(section.section_path) + len(section.section_title) + len(section.summary), limit)
        yield None
    resolved: dict[str, tuple[int, int]] = {}
    maximumDepth: int = settings.KNOWHERE_INDEXING_MAXIMUM_HIERARCHY_DEPTH
    for section in sections:
        pending: list[str] = []
        visiting: set[str] = set()
        current: str | None = section.section_id
        while current in parents and current not in resolved:
            if current in visiting:
                raise LexicalIndexError("Indexing hierarchy contains a parent cycle")
            visiting.add(current)
            pending.append(current)
            if len(pending) > maximumDepth:
                raise LexicalIndexError("Indexing hierarchy exceeds its depth limit", isSizeLimit=True)
            current = parents[current].parent_section_id
            yield None
        depth, characters = resolved.get(current or "", (0, 0))
        for identity in reversed(pending):
            title: str = parents[identity].section_title
            depth += 1
            if depth > maximumDepth:
                raise LexicalIndexError("Indexing hierarchy exceeds its depth limit", isSizeLimit=True)
            characters += (3 if characters and title else 0) + len(title)
            _checkSize(characters, limit)
            resolved[identity] = depth, characters
            yield None
    for chunk in chunks:
        _checkSize(len(chunk.content) + len(chunk.source_chunk_path) + len(chunk.file_path), limit)
        yield from _validateMetadata(chunk.metadata, limit)
        yield None


def _validateUnits(provider: KnowhereProvider, sections: Sequence[SectionRow], limit: int) -> Iterator[None]:
    for section in sections:
        characters: int = 0
        for unit in provider.iterateSelfUnits(section.section_id):
            # Bound the whole aggregate before raw scoring joins it. Empty
            # records still consume work, so a giant empty self span is bounded.
            characters += len(provider.unit_text(unit)) + 1
            _checkSize(characters, limit)
            yield None
        yield None


def _createUnit(provider: KnowhereProvider, paths: dict[str, str], row: dict,
                documentId: str, revisionId: str, order: int) -> LexicalUnit:
    sectionId: str = str(row["section_id"])
    types: set[str] = {chunk.chunk_type for chunk in provider.iterateSelfUnits(sectionId)}
    return LexicalUnit(documentId=documentId, revisionId=revisionId, unitId=str(row["chunk_id"]),
        sectionId=sectionId, pathTokens=str(row.get("path_search_text") or ""),
        contentTokens=str(row.get("content_search_text") or ""), sectionPath=paths.get(sectionId, ""),
        sortOrder=order, hasImage="image" in types, hasTable="table" in types)


def _tokenizeSteps(row: dict, limit: int) -> Iterator[None]:
    content: str = str(row["content"])
    path: str = str(row["path_text"])
    title: str = str(row["section_title"])
    _checkSize(len(content), limit)
    _checkSize(len(path) + len(title) + 1, limit)
    def validateCjkSpan(span: str) -> None:
        maximum: int = settings.KNOWHERE_INDEXING_MAXIMUM_CJK_SPAN_CHARACTERS
        if len(span) > maximum:
            raise LexicalIndexError(f"Indexing uninterrupted CJK tokenizer span exceeds its character limit ({len(span)} > {maximum})", isSizeLimit=True)
    row["path_search_text"] = yield from _iterate_path_search_text(section_path=path,
        section_title=title, validateCjkSpan=validateCjkSpan)
    yield None
    row["content_search_text"] = yield from _iterate_content_search_text(content, validateCjkSpan=validateCjkSpan)
    yield None


class LexicalPreparation:
    @staticmethod
    def getCompilerFingerprint() -> str:
        return _getCompilerFingerprint()

    @staticmethod
    def warmCompiler() -> None:
        """Run once during startup before accepting API traffic, never in a pool."""
        _getCompilerFingerprint()
        build_content_search_text("词典初始化 compiler startup")

    @staticmethod
    def prepare(*, documentId: str, revisionId: str, sections: Sequence[SectionRow],
                chunks: Sequence[UnitRow], checkpoint: Callable[[], None] | None = None) -> LexicalInput:
        def check() -> None:
            if checkpoint is not None:
                checkpoint()
        check()
        limit: int = settings.KNOWHERE_INDEXING_MAXIMUM_UNIT_CHARACTERS
        for _ in _validateRows(sections, chunks, limit):
            check()
        provider: KnowhereProvider = KnowhereProvider(doc_id=documentId, sections=sections, units=chunks, checkpoint=checkpoint)
        for _ in _validateUnits(provider, sections, limit):
            check()
        paths: dict[str, str] = {}
        for section in sections:
            paths[section.section_id] = section.section_path
            check()
        prepared: list[LexicalUnit] = []
        for row in _iterate_score_units(ProviderToolSpace(provider), documentId, tokenize=False):
            check()
            if row is None:
                continue
            for _ in _tokenizeSteps(row, limit):
                check()
            prepared.append(_createUnit(provider, paths, row, documentId, revisionId, len(prepared)))
        check()
        return LexicalInput(compilerFingerprint=_getCompilerFingerprint(), units=tuple(prepared))

    @staticmethod
    async def prepareAsync(*, documentId: str, revisionId: str, sections: Sequence[SectionRow],
                           chunks: Sequence[UnitRow], deadline: IndexingDeadline | None = None) -> LexicalInput:
        checkpoint: Callable[[], Awaitable[None]] = CompilationCheckpoint(deadline).checkAsync

        await checkpoint()
        limit: int = settings.KNOWHERE_INDEXING_MAXIMUM_UNIT_CHARACTERS
        for _ in _validateRows(sections, chunks, limit):
            await checkpoint()
        provider: KnowhereProvider = await KnowhereProvider.createAsync(doc_id=documentId,
            sections=sections, units=chunks, checkpoint=checkpoint)
        for _ in _validateUnits(provider, sections, limit):
            await checkpoint()
        paths: dict[str, str] = {}
        for section in sections:
            paths[section.section_id] = section.section_path
            await checkpoint()
        prepared: list[LexicalUnit] = []
        for row in _iterate_score_units(ProviderToolSpace(provider), documentId, tokenize=False):
            await checkpoint()
            if row is None:
                continue
            for _ in _tokenizeSteps(row, limit):
                await checkpoint()
            prepared.append(_createUnit(provider, paths, row, documentId, revisionId, len(prepared)))
        await checkpoint()
        return LexicalInput(compilerFingerprint=_getCompilerFingerprint(), units=tuple(prepared))
