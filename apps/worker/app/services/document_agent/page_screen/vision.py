"""VLM calls over numbered page grids, shared by the page screen and the slide route."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol, cast

from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.page_screen.config import GridLayout

VlmCall = Callable[[str, list[str], str], dict[str, Any]]
"""``(prompt, base64 PNG images, usage task) -> parsed JSON object``."""


class PageGrids(Protocol):
    def grids(
        self, pages: list[int], layout: GridLayout, *, name: str
    ) -> list[str]: ...


def vision_call(
    ctx: ToolContext, *, max_tokens: int, usage: dict[str, int], usage_scope: str
) -> VlmCall:
    """A JSON-object VLM call that adds its calls and tokens to ``usage``."""
    from shared.services.ai.llm_overrides import get_vision_client

    client, model = get_vision_client(requested_model=ctx.settings.get("vlm_model"))

    def call(prompt: str, images: list[str], task: str) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image}"},
            }
            for image in images
        )
        raw, call_usage = client.chat_completion_with_usage(
            messages=cast(Any, [{"role": "user", "content": content}]),
            model=model,
            temperature=0.0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            usage_task=f"document_agent.{usage_scope}.{task}",
        )
        usage["calls"] += 1
        usage["tokens"] += int(call_usage.get("total_tokens", 0) or 0)
        answer = json.loads(raw)
        if not isinstance(answer, dict):
            raise ValueError(f"{usage_scope} {task} output must be a JSON object")
        return answer

    return call


def parallel_vlm(
    requests: list[tuple[str, list[str], str]], *, vlm: VlmCall, concurrency: int
) -> list[dict[str, Any]]:
    if not requests:
        return []
    from gevent.pool import Pool as GeventPool

    pool = GeventPool(size=min(concurrency, len(requests)))
    jobs = [pool.spawn(vlm, prompt, images, task) for prompt, images, task in requests]
    pool.join(raise_error=True)
    return [job.get() for job in jobs]


def page_numbers(raw: Any, allowed: set[int]) -> list[int]:
    """The integers in ``raw`` that are ``allowed`` page numbers, sorted and unique."""
    numbers = {as_int(item) for item in (raw if isinstance(raw, list) else [])}
    return sorted(number for number in numbers if number in allowed)


def as_int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
