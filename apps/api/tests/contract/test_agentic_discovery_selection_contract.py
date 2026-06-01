from shared.services.retrieval.agentic.core.types import DocTreeNode
from shared.services.retrieval.agentic.discovery.selection import (
    _build_discovery_path_selections,
    _project_discovery_hints,
)


def test_root_discovery_hint_is_projected_for_llm_selection() -> None:
    hint_lines, hint_by_path, excluded_hints = _project_discovery_hints(
        [
            {
                "section_path": "Root",
                "chunk_id": "chunk_root_relevant",
                "summary": "document-level market chart",
            }
        ],
        exclude_paths=None,
    )

    assert hint_lines == [
        '▸ path="Root"',
        "    document-level market chart",
    ]
    assert hint_by_path["Root"]["chunk_id"] == "chunk_root_relevant"


def test_root_discovery_hint_without_llm_selection_does_not_hydrate() -> None:
    node = DocTreeNode()

    path_selections, chunk_refs = _build_discovery_path_selections(
        selections=[],
        hint_by_path={
            "Root": {
                "section_path": "Root",
                "chunk_id": "chunk_root_relevant",
            }
        },
        document_id="doc_root",
        node=node,
    )

    assert path_selections == []
    assert chunk_refs == []
    assert node.confidence == {}


def test_explicit_root_discovery_selection_with_chunk_id_uses_exact_chunk_ref() -> None:
    node = DocTreeNode()

    path_selections, chunk_refs = _build_discovery_path_selections(
        selections=[{"path": "Root", "confidence": 0.91}],
        hint_by_path={
            "Root": {
                "section_path": "Root",
                "chunk_id": "chunk_root_relevant",
            }
        },
        document_id="doc_root",
        node=node,
    )

    assert path_selections == []
    assert chunk_refs == [
        {
            "document_id": "doc_root",
            "chunk_id": "chunk_root_relevant",
            "section_path": "Root",
        }
    ]
    assert node.confidence["Root"] == 0.91


def test_explicit_root_discovery_selection_without_chunk_id_keeps_path_fallback() -> None:
    node = DocTreeNode()

    path_selections, chunk_refs = _build_discovery_path_selections(
        selections=[{"path": "Root", "confidence": 0.7}],
        hint_by_path={
            "Root": {
                "section_path": "Root",
                "chunk_id": "",
            }
        },
        document_id="doc_root",
        node=node,
    )

    assert path_selections == [
        {
            "path": "Root",
            "confidence": 0.7,
            "hydrate_mode": "self_only",
        }
    ]
    assert chunk_refs == []
    assert node.confidence["Root"] == 0.7
