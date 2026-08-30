"""Contracts for persisted map-unit term scoring."""

from __future__ import annotations

from shared.services.retrieval.nav.nav_knowhere import ReadOnlyChunkStore


class _FakeCursor:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows = rows
        self.statement = ""
        self.parameters: object = None

    def execute(self, statement: str, parameters: object) -> None:
        self.statement = statement
        self.parameters = parameters

    def fetchall(self) -> list[tuple[str, str]]:
        return self.rows


def _store() -> ReadOnlyChunkStore:
    return ReadOnlyChunkStore.__new__(ReadOnlyChunkStore)


def test_term_scores_keep_literal_substring_and_token_hit_semantics() -> None:
    cursor = _FakeCursor(
        [
            ("unit-full", "prefix alpha beta suffix"),
            ("unit-token", "prefix alpha gamma suffix"),
        ]
    )
    queries = ["alpha beta", "", "alpha"]
    query_tokens = {
        "alpha beta": ["alpha", "beta"],
        "": [],
        "alpha": ["alpha"],
    }

    scores = _store()._load_term_scores(
        cursor,  # type: ignore[arg-type]
        map_unit_ids=["unit-full", "unit-token", "unit-miss"],
        queries=queries,
        query_tokens_by_query=query_tokens,
    )

    assert scores == {
        "unit-full": (100.0, 0.0, 100.0),
        "unit-token": (1.0, 0.0, 100.0),
    }
    assert "LIKE ANY" in cursor.statement
    assert "POSITION" not in cursor.statement
    parameters = cursor.parameters
    assert isinstance(parameters, tuple)
    assert parameters[0] == ["unit-full", "unit-token", "unit-miss"]
    assert parameters[1] == ["%alpha beta%", "%alpha%", "%beta%"]


def test_long_query_uses_constant_shape_candidate_sql() -> None:
    cursor = _FakeCursor([])
    tokens = [f"token-{index}" for index in range(300)]
    query = " ".join(tokens)

    _store()._load_term_scores(
        cursor,  # type: ignore[arg-type]
        map_unit_ids=["unit-1"],
        queries=[query],
        query_tokens_by_query={query: tokens},
    )

    assert cursor.statement.count("POSITION") == 0
    assert "LIKE ANY" not in cursor.statement
    parameters = cursor.parameters
    assert isinstance(parameters, tuple)
    assert parameters == (["unit-1"],)
