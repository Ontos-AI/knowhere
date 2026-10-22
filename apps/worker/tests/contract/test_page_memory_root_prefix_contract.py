"""Opening physical pages survive skeleton resolution through ZIP publication."""

from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.structure.anchoring_primitives import (
    SkeletonAnchor,
    serialize_skeleton_anchor,
    serialize_title_node,
)
from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    TitleNode,
)
from app.services.page_memory import memory_service, page_renderer, page_tagger
from app.services.page_memory.page_tagger import PageTagResult
from shared.models.schemas.page_memory_config import PageMemoryConfig
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
from shared.services.retrieval.search.lexical_text import section_path_from_chunk_path
from shared.services.storage.zip_chunk_schema import ZipChunkSchemaBuilder
from shared.services.storage.zip_doc_navigation import ZipDocNavigationBuilder


@pytest.mark.parametrize(
    ("page_count", "chapter_pages", "toc_pages"),
    [
        (9, [5, 6, 7, 8, 9], []),
        (407, [20], [17, 18]),
        (9, [5], [2, 7]),
        (9, [1, 5], []),
        (9, [], []),
    ],
    ids=[
        "meta-outline-prefix",
        "spacex-prefix-includes-toc",
        "chapter-toc-policy-unchanged",
        "chapter-on-first-page",
        "no-toc-fallback",
    ],
)
def test_opening_pages_keep_root_ownership_and_page_citation_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_count: int,
    chapter_pages: list[int],
    toc_pages: list[int],
) -> None:
    filename: str = "document.pdf"
    titles: list[str] = [f"Chapter {page}" for page in chapter_pages]
    anatomy: SimpleNamespace = SimpleNamespace(
        page_count=page_count,
        page_features=[],
        page_labels=[],
        toc_result=SimpleNamespace(toc_pages=toc_pages),
        toc_hierarchies=[
            {
                "toc_range": [17, 18] if toc_pages else [1, 1],
                "toc_range_unit": "page",
                "toc_with_level": [
                    {"heading": title, "level": 1, "page_number": page}
                    for title, page in zip(titles, chapter_pages)
                ],
            }
        ]
        if chapter_pages
        else [],
        skeleton_nodes=[
            serialize_title_node(TitleNode(title=title, level=1, printed_page=page))
            for title, page in zip(titles, chapter_pages)
        ],
        skeleton_anchor=serialize_skeleton_anchor(
            SkeletonAnchor(
                offset=0,
                offset_status="ok",
                match_overrides={
                    (title,): TitleMatch(
                        page=page,
                        source="anchored",
                        matched_line=title,
                        candidates=[page],
                        evidence={},
                    )
                    for title, page in zip(titles, chapter_pages)
                },
                null_page_report=[],
                bulk_count=len(titles),
                pruned_count=0,
                locate_method="offset_guided_bulk",
            )
        ),
    )
    page_texts: dict[int, str] = {
        page: f"Body on physical page {page}." for page in range(1, page_count + 1)
    }
    profile: SimpleNamespace = SimpleNamespace(
        page_count=page_count,
        anatomy=anatomy,
        page_full_text_cache=page_texts,
    )

    def render_pages(
        ctx: object, pages: list[int], **kwargs: object
    ) -> list[dict[str, object]]:
        image_directory: Path = tmp_path / str(kwargs["folder_name"])
        image_directory.mkdir(parents=True, exist_ok=True)
        rendered: list[dict[str, object]] = []
        for page in pages:
            image_path: Path = image_directory / f"page-{page}.png"
            Image.new("RGB", (2, 3), color="white").save(image_path)
            rendered.append({"page": page, "png_path": str(image_path)})
        return rendered

    def tag_pages(**kwargs: object) -> list[PageTagResult]:
        pages = kwargs["pages"]
        assert isinstance(pages, list)
        return [
            PageTagResult(page_index=page.page_index, summary=f"Page {page.page_index}")
            for page in pages
        ]

    def retain_chapter_titles(**kwargs: object) -> list[PageTagResult]:
        pages = kwargs["fat_leaf_pages"]
        assert isinstance(pages, set)
        assert not (pages & set(range(1, chapter_pages[0])))
        tags = kwargs["tag_results"]
        assert isinstance(tags, list)
        return tags

    monkeypatch.delenv("IMAGE_MODEL", raising=False)
    monkeypatch.setattr(page_renderer, "render_pages", render_pages)
    monkeypatch.setattr(page_tagger, "tag_pages", tag_pages)
    monkeypatch.setattr(page_tagger, "tag_page_titles", retain_chapter_titles)

    dataframe = memory_service._build_page_dataframe(
        pdf_path=str(tmp_path / filename),
        filename=filename,
        output_dir=str(tmp_path),
        profile=profile,
        verdict="page",
        page_memory_config=PageMemoryConfig(
            scope_concurrency=3 if page_count == 407 else 1,
            fine_min_pages=4 if chapter_pages and chapter_pages[0] > 1 else page_count,
            asset_extraction_enabled=False,
        ),
    )
    chunks = dataframe_to_chunks(dataframe)
    published: list[dict[str, object]] = ZipChunkSchemaBuilder().format_chunks(
        chunks,
        image_files_map={},
        table_files_map={},
    )
    expected_prefix: list[int] = (
        list(range(1, chapter_pages[0]))
        if chapter_pages
        else list(range(1, page_count + 1))
    )
    root_path: str = filename if chapter_pages else f"{filename}/Root"
    root_chunks = [chunk for chunk in published if chunk["path"] == root_path]
    assert len(root_chunks) == bool(expected_prefix)
    if expected_prefix:
        root_metadata = root_chunks[0]["metadata"]
        assert root_metadata["page_nums"] == expected_prefix
        assert [
            asset["page_num"] for asset in root_metadata["page_assets"]
        ] == expected_prefix
        assert all(
            page_texts[page] in root_chunks[0]["content"] for page in expected_prefix
        )
        assert (
            section_path_from_chunk_path(root_path, source_file_name=filename) == "Root"
        )
        navigation = ZipDocNavigationBuilder().build_doc_nav(published, filename)
        root_sections = [
            section
            for section in navigation["sections"]
            if section["path"] == root_path
        ]
        assert len(root_sections) == 1
        assert root_sections[0]["chunk_count"] == len(expected_prefix)
        if chapter_pages:
            assert not any(chunk["path"] == f"{filename}/Root" for chunk in published)

    assert [chunk["path"] for chunk in published if chunk["path"] != root_path] == [
        f"{filename}/{title}" for title in titles
    ]
    covered_pages: set[int] = {
        page for chunk in published for page in chunk["metadata"]["page_nums"]
    }
    assert covered_pages == set(range(1, page_count + 1)) - (
        set(toc_pages) - set(expected_prefix)
    )
    for chunk in published:
        for asset in chunk["metadata"]["page_assets"]:
            assert (tmp_path / asset["artifact_ref"]).is_file()


def test_staged_prefix_scope_retains_root_identity_and_printed_toc(
    tmp_path: Path,
) -> None:
    from app.services.page_memory._serialization import serialize_hierarchy_artifact
    from app.services.page_memory.skeleton_extractor import SectionSkeleton

    prefix: SectionSkeleton = SectionSkeleton(
        section_path="spacex.pdf",
        level=0,
        start_page=1,
        end_page=19,
        title="Root",
        parent_path=None,
        evidence={"source": "document_prefix"},
    )
    artifact_path: Path = tmp_path / "fine_hierarchy.json"
    artifact_path.write_text(json.dumps(serialize_hierarchy_artifact([prefix])))
    repository: Path = Path(__file__).resolve().parents[4]
    # Staged helpers monkey-patch gevent, so exercise their public file boundary
    # in a separate process rather than modifying the contract runner's runtime.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from pathlib import Path
sys.path.insert(0, 'apps/worker/scripts/page_memory')
from _debug_pm_shared import load_hierarchy_artifact, build_debug_coarse_scopes
from debug_pm_stage3_scope_fine_hierarchy import _resolve_scope_processing_pages
from debug_pm_stage4_assets import _load_scope_asset_context
from app.services.page_memory._serialization import derive_hierarchy_page_scope, build_hierarchy_tree
from app.services.page_memory.fine_hierarchy import compute_fat_leaf_pages
from types import SimpleNamespace
path = Path(sys.argv[1])
_, skeletons = load_hierarchy_artifact(path)
assert skeletons[0].level == 0
assert skeletons[0].evidence['source'] == 'document_prefix'
assert derive_hierarchy_page_scope(skeletons=skeletons, page_count=407) == list(range(1, 20))
assert build_hierarchy_tree(skeletons) == {}
assert compute_fat_leaf_pages(skeletons, min_pages=4, toc_pages=[17, 18]) == set()
anatomy = SimpleNamespace(toc_result=SimpleNamespace(toc_pages=[17, 18]))
scopes = build_debug_coarse_scopes(skeletons=skeletons, filename='spacex.pdf', page_count=407, anatomy=anatomy)
assert len(scopes) == 1
assert scopes[0]['processing_pages'] == list(range(1, 20))
assert scopes[0]['excluded_toc_pages'] == []
pages, excluded = _resolve_scope_processing_pages(scope_meta={}, skeletons=skeletons, page_count=407, toc_pages=[17, 18])
assert pages == list(range(1, 20))
assert excluded == []
context = _load_scope_asset_context(scope_id='p1-19', scope_dir=path.parent, page_count=407, toc_pages=[17, 18])
assert context.pages == list(range(1, 20))
""",
            str(artifact_path),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
