"""Bound provider output while preserving hierarchy context between batches."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Protocol

from loguru import logger
from openai.types.chat import ChatCompletionMessageParam

from shared.services.ai.prompt_service import build_prompt


class _HierarchyClient(Protocol):
    def chat_completion(
        self,
        *,
        messages: list[ChatCompletionMessageParam],
        model: str | None,
        max_tokens: int,
        temperature: float,
        usage_task: str,
    ) -> str: ...


class _HierarchyResponseError(ValueError):
    """The provider did not classify exactly the requested candidate IDs."""


class PageHierarchyLevelResolver:
    """Classify every heading or noise row, with bounded recovery on bad output.

    Ancestors retain their relative levels across batches. Invalid responses
    are retried in smaller batches, never interpreted as an empty hierarchy.
    Exhausted recovery fails the import so a published revision stays intact.
    """

    def __init__(
        self,
        *,
        client: _HierarchyClient,
        model: str | None,
        max_tokens: int,
        max_depth: int,
        coarse_context: str,
    ) -> None:
        self._client: _HierarchyClient = client
        self._model: str | None = model
        self._max_tokens: int = max_tokens
        self._max_depth: int = max_depth
        self._coarse_context: str = coarse_context

    def resolve_levels(
        self, candidates: Sequence[Mapping[str, object]]
    ) -> dict[int, int]:
        # A response row needs ID, level and JSON punctuation. Leave ample
        # completion headroom; cap input rows as well as the token estimate.
        batch_size: int = max(1, min(64, self._max_tokens // 32))
        levels: dict[int, int] = {}
        for start in range(0, len(candidates), batch_size):
            batch: Sequence[Mapping[str, object]] = candidates[
                start : start + batch_size
            ]
            levels.update(
                self._resolve_batch(
                    batch,
                    preceding_candidates=candidates[:start],
                    preceding_levels=levels,
                )
            )
        return levels

    def _resolve_batch(
        self,
        candidates: Sequence[Mapping[str, object]],
        *,
        preceding_candidates: Sequence[Mapping[str, object]],
        preceding_levels: Mapping[int, int],
        can_retry: bool = True,
    ) -> dict[int, int]:
        prompt: str
        temperature: float
        completion_budget: int
        context: str = self._build_context(preceding_candidates, preceding_levels)
        prompt, temperature, _, completion_budget = build_prompt(
            "page-memory-hierarchy",
            json.dumps(list(candidates), ensure_ascii=False, indent=2),
            "",
            paras={
                "max_depth": self._max_depth,
                "max_tokens": self._max_tokens,
                "coarse_context": context,
            },
        )
        answer: str = self._client.chat_completion(
            messages=[
                {"role": "system", "content": "you are a document structure expert"},
                {"role": "user", "content": prompt},
            ],
            model=self._model,
            max_tokens=completion_budget,
            temperature=temperature,
            usage_task="page_memory.hierarchy",
        )
        try:
            return self._parse_levels(answer, candidates)
        except _HierarchyResponseError as err:
            logger.warning(
                "[page_memory.fine_hierarchy] invalid response for {} candidates; {}",
                len(candidates),
                err,
            )
            if len(candidates) == 1:
                if can_retry:
                    return self._resolve_batch(
                        candidates,
                        preceding_candidates=preceding_candidates,
                        preceding_levels=preceding_levels,
                        can_retry=False,
                    )
                raise
            midpoint: int = len(candidates) // 2
            left: Sequence[Mapping[str, object]] = candidates[:midpoint]
            levels: dict[int, int] = self._resolve_batch(
                left,
                preceding_candidates=preceding_candidates,
                preceding_levels=preceding_levels,
            )
            levels.update(
                self._resolve_batch(
                    candidates[midpoint:],
                    preceding_candidates=[*preceding_candidates, *left],
                    preceding_levels={**preceding_levels, **levels},
                )
            )
            return levels

    def _parse_levels(
        self,
        answer: str,
        candidates: Sequence[Mapping[str, object]],
    ) -> dict[int, int]:
        content: str = answer.strip()
        try:
            if content.startswith("```") and content.endswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            response: object = json.loads(content)
        except (json.JSONDecodeError, IndexError) as err:
            raise _HierarchyResponseError(
                "Hierarchy JSON is incomplete or malformed."
            ) from err
        if not isinstance(response, list):
            raise _HierarchyResponseError("Hierarchy must be an array.")
        expected_ids: set[int] = {int(str(candidate["id"])) for candidate in candidates}
        levels: dict[int, int] = {}
        for row in response:
            if not isinstance(row, dict):
                raise _HierarchyResponseError("Hierarchy row must be an object.")
            identifier: object = row.get("id")
            level: object = row.get("level")
            if type(identifier) is not int or type(level) is not int:
                raise _HierarchyResponseError(
                    "Hierarchy IDs and levels must be integers."
                )
            if (
                identifier not in expected_ids
                or identifier in levels
                or not 0 <= level <= self._max_depth
            ):
                raise _HierarchyResponseError(
                    "Hierarchy contains duplicate, unknown or invalid rows."
                )
            levels[identifier] = level
        if set(levels) != expected_ids:
            raise _HierarchyResponseError(
                "Hierarchy did not classify every requested heading."
            )
        return levels

    def _build_context(
        self,
        candidates: Sequence[Mapping[str, object]],
        levels: Mapping[int, int],
    ) -> str:
        ancestors: list[str] = []
        ancestor_levels: list[int] = []
        for candidate in candidates:
            identifier: int = int(str(candidate["id"]))
            level: int = levels[identifier]
            if level == 0:
                continue
            while ancestor_levels and ancestor_levels[-1] >= level:
                ancestor_levels.pop()
                ancestors.pop()
            ancestor_levels.append(level)
            ancestors.append(f"level={level}, heading={candidate['heading']}")
        return self._coarse_context + (
            "\nPreviously classified ancestors in reading order (context only; do not output them):\n"
            + "\n".join(ancestors)
            + "\nContinue their relative levels; a batch boundary does not start a new subtree."
            if ancestors
            else ""
        )
