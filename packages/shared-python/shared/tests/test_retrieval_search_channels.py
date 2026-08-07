"""Unit coverage for bounded PostgreSQL candidates in lexical channels."""

from __future__ import annotations

from typing import Any

import pytest
from pytest import MonkeyPatch

from shared.services.retrieval.search import channels


class _FakeRow:
	def __init__(self, **values: Any) -> None:
		self._mapping = values


class _FakeResult:
	def __init__(self, rows: list[_FakeRow]) -> None:
		self._rows = rows

	def all(self) -> list[_FakeRow]:
		return self._rows


class _FakeSession:
	def __init__(self, *result_sets: list[_FakeRow]) -> None:
		self.result_sets = list(result_sets)
		self.calls: list[tuple[str, dict[str, Any]]] = []

	async def execute(
		self,
		statement: object,
		params: dict[str, Any],
	) -> _FakeResult:
		self.calls.append((str(statement), dict(params)))
		return _FakeResult(self.result_sets.pop(0))


class _FakeLogger:
	def __init__(self) -> None:
		self.messages: list[str] = []

	def info(self, message: str) -> None:
		self.messages.append(message)


def _row(
	*,
	id_row: int,
	content_search_text: str = "alpha beta",
	path_search_text: str = "root alpha",
) -> _FakeRow:
	return _FakeRow(
		id=f"row-{id_row}",
		chunk_id=f"chunk-{id_row}",
		document_id="doc-1",
		section_id=f"section-{id_row}",
		chunk_type="text",
		content=f"content {id_row}",
		content_search_text=content_search_text,
		path_search_text=path_search_text,
		section_path=f"Root / Section {id_row}",
	)


@pytest.mark.asyncio
async def test_content_channel_uses_bounded_or_fts_after_scope_filters(
	monkeypatch: MonkeyPatch,
) -> None:
	monkeypatch.setenv("RETRIEVAL_POSTGRES_FTS_CANDIDATE_LIMIT", "7")
	db = _FakeSession([_row(id_row=1)])

	rows = await channels.content_channel(
		db,  # type: ignore[arg-type]
		user_id="user-1",
		namespace="knowledge",
		query="alpha beta",
		top_k=3,
		exclude_document_ids=["doc-old"],
		exclude_sections=[
			{"document_id": "doc-1", "section_path": "Root / Hidden"}
		],
		allowed_chunk_types={"text"},
		signal_paths=["Root"],
		filter_mode="keep",
	)

	assert len(rows) == 1
	assert len(db.calls) == 1
	sql, params = db.calls[0]
	assert "sc.content_search_tsv @@" in sql
	assert "websearch_to_tsquery('simple', :fts_or_query)" in sql
	assert "ORDER BY ts_rank_cd" in sql
	assert "LIMIT :fts_candidate_limit" in sql
	assert sql.index("LOWER(dc.chunk_type)") < sql.index("LIMIT :fts_candidate_limit")
	assert sql.index("LOWER(COALESCE(ds.section_path") < sql.index(
		"LIMIT :fts_candidate_limit"
	)
	assert sql.index("POSITION(:_exc_section_path_0") < sql.index(
		"LIMIT :fts_candidate_limit"
	)
	assert params["fts_or_query"] == '"alpha" OR "beta"'
	assert params["fts_candidate_limit"] == 7
	assert params["excluded_doc_ids"] == ["doc-old"]
	assert params["_exc_section_doc_0"] == "doc-1"
	assert params["_exc_section_path_0"] == "Root / Hidden"


@pytest.mark.asyncio
async def test_path_channel_uses_path_tsv_and_python_bm25_final_reranker(
	monkeypatch: MonkeyPatch,
) -> None:
	monkeypatch.setenv("RETRIEVAL_POSTGRES_FTS_CANDIDATE_LIMIT", "4")
	candidate_rows = [_row(id_row=index) for index in range(8)]
	db = _FakeSession(candidate_rows[:4])
	captured: dict[str, Any] = {}

	def fake_rank(
		rows: list[dict[str, Any]],
		query_tokens: list[str],
		*,
		search_field: str,
	) -> list[dict[str, Any]]:
		captured["candidate_count"] = len(rows)
		captured["query_tokens"] = query_tokens
		captured["search_field"] = search_field
		return list(reversed(rows))

	monkeypatch.setattr(channels, "rank_rows_by_bm25", fake_rank)

	rows = await channels.path_channel(
		db,  # type: ignore[arg-type]
		user_id="user-1",
		namespace="knowledge",
		query="alpha beta",
		top_k=2,
		exclude_document_ids=[],
		exclude_sections=[],
	)

	sql, params = db.calls[0]
	assert "sc.path_search_tsv @@" in sql
	assert "sc.content_search_tsv @@" not in sql
	assert params["fts_candidate_limit"] == 4
	assert captured == {
		"candidate_count": 4,
		"query_tokens": ["alpha", "beta"],
		"search_field": "path_search_text",
	}
	assert [row["id"] for row in rows] == ["row-3", "row-2"]


@pytest.mark.asyncio
async def test_bm25_channel_uses_bounded_fallback_and_logs_metrics(
	monkeypatch: MonkeyPatch,
) -> None:
	monkeypatch.setenv("RETRIEVAL_POSTGRES_FTS_CANDIDATE_LIMIT", "5")
	db = _FakeSession([], [_row(id_row=1)])
	fake_logger = _FakeLogger()
	monkeypatch.setattr(channels, "logger", fake_logger)

	rows = await channels.content_channel(
		db,  # type: ignore[arg-type]
		user_id="user-1",
		namespace="knowledge",
		query="alpha",
		top_k=2,
		exclude_document_ids=[],
		exclude_sections=[],
	)

	assert len(rows) == 1
	assert len(db.calls) == 2
	fts_sql, fts_params = db.calls[0]
	fallback_sql, fallback_params = db.calls[1]
	assert "@@ websearch_to_tsquery" in fts_sql
	assert "@@ websearch_to_tsquery" not in fallback_sql
	assert "LIMIT :fts_candidate_limit" in fallback_sql
	assert fts_params["fts_candidate_limit"] == 5
	assert fallback_params["fts_candidate_limit"] == 5
	assert len(fake_logger.messages) == 1
	assert "channel=content" in fake_logger.messages[0]
	assert "scoped_count=unavailable" in fake_logger.messages[0]
	assert "candidate_count=1" in fake_logger.messages[0]
	assert "candidate_limit=5" in fake_logger.messages[0]
	assert "ranked_count=1" in fake_logger.messages[0]
	assert "duration_ms=" in fake_logger.messages[0]
	assert "fallback_used=true" in fake_logger.messages[0]


@pytest.mark.asyncio
async def test_empty_or_unsafe_query_does_not_load_fallback_candidates() -> None:
	db = _FakeSession()

	rows = await channels.content_channel(
		db,  # type: ignore[arg-type]
		user_id="user-1",
		namespace="knowledge",
		query="!!! ---",
		top_k=2,
		exclude_document_ids=[],
		exclude_sections=[],
	)

	assert rows == []
	assert db.calls == []
