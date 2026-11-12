from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Protocol

from loguru import logger

from shared.services.http.client_pool import get_sync_client

_LUNA_MODEL = "openai/gpt-6-luna-decisions"
_LUNA_EVALUATE_URL = "https://ai-gateway.vercel.sh/v1/evaluate"

_TEXT_JUDGE_SYSTEM_PROMPT = """You answer multiple-choice questions about the given state.
For every question, pick exactly one of its criteria keys.
Return JSON only: an object mapping each question id to the chosen key."""


class ChoiceJudge(Protocol):
    def choose(
        self,
        *,
        state: Mapping[str, object],
        questions: Mapping[str, Mapping[str, object]],
        required_ids: Sequence[str],
        allowed: Mapping[str, frozenset[str]],
    ) -> Mapping[str, str] | None:
        """Return question id → choice, or None to fail closed."""


def _gateway_api_key() -> str:
    from shared.core.config import settings

    vercel = str(getattr(settings, "VERCEL_KEY", "") or "").strip()
    if vercel:
        return vercel
    return str(os.environ.get("AI_GATEWAY_API_KEY", "") or "").strip()


def _validated_choices(
    answers: Mapping[str, object],
    *,
    required_ids: Sequence[str],
    allowed: Mapping[str, frozenset[str]],
) -> dict[str, str] | None:
    chosen: dict[str, str] = {}
    for question_id in required_ids:
        choice = answers.get(question_id)
        allowed_choices = allowed.get(question_id, frozenset())
        if not isinstance(choice, str) or choice not in allowed_choices:
            return None
        chosen[question_id] = choice
    return chosen


class GatewayLunaJudge:
    """Ask the Vercel AI Gateway Luna decisions model."""

    def choose(
        self,
        *,
        state: Mapping[str, object],
        questions: Mapping[str, Mapping[str, object]],
        required_ids: Sequence[str],
        allowed: Mapping[str, frozenset[str]],
    ) -> Mapping[str, str] | None:
        from shared.core.config import settings

        api_key = _gateway_api_key()
        if not api_key or not questions:
            return None
        try:
            timeout_seconds = float(
                getattr(settings, "OPENAI_CLIENT_TIMEOUT", 300) or 300
            )
            response = get_sync_client().post(
                _LUNA_EVALUATE_URL,
                json={
                    "model": _LUNA_MODEL,
                    "state": state,
                    "questions": questions,
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=timeout_seconds,
            )
            if not response.is_success:
                logger.warning(
                    "Luna evaluate HTTP {}: {}",
                    response.status_code,
                    response.reason_phrase,
                )
                return None
            payload = response.json()
        except Exception as exc:
            logger.warning("Luna evaluate failed: {}", exc)
            return None
        answers = payload.get("answers") if isinstance(payload, dict) else None
        if not isinstance(answers, dict):
            return None
        return _validated_choices(
            {
                question_id: item.get("choice")
                for question_id, item in answers.items()
                if isinstance(item, dict)
            },
            required_ids=required_ids,
            allowed=allowed,
        )


class TextLlmChoiceJudge:
    """Ask the configured text LLM the same choice questions as JSON."""

    def __init__(self, *, usage_task: str) -> None:
        self._usage_task = usage_task

    def choose(
        self,
        *,
        state: Mapping[str, object],
        questions: Mapping[str, Mapping[str, object]],
        required_ids: Sequence[str],
        allowed: Mapping[str, frozenset[str]],
    ) -> Mapping[str, str] | None:
        from shared.services.ai.llm_overrides import get_text_client
        from shared.services.ai.response_process_service import eval_response

        if not questions:
            return None
        try:
            client, _model = get_text_client()
            raw = client.chat_completion(
                messages=[
                    {"role": "system", "content": _TEXT_JUDGE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"state": state, "questions": questions},
                            ensure_ascii=False,
                        ),
                    },
                ],
                usage_task=self._usage_task,
            )
            answers = eval_response(raw)
        except Exception as exc:
            logger.warning("Text LLM choice judge {} failed: {}", self._usage_task, exc)
            return None
        if not isinstance(answers, dict):
            return None
        return _validated_choices(answers, required_ids=required_ids, allowed=allowed)


class FallbackChoiceJudge:
    """Use the first judge that returns a complete, valid answer set."""

    def __init__(self, judges: Sequence[ChoiceJudge]) -> None:
        self._judges = tuple(judges)

    def choose(
        self,
        *,
        state: Mapping[str, object],
        questions: Mapping[str, Mapping[str, object]],
        required_ids: Sequence[str],
        allowed: Mapping[str, frozenset[str]],
    ) -> Mapping[str, str] | None:
        for judge in self._judges:
            chosen = judge.choose(
                state=state,
                questions=questions,
                required_ids=required_ids,
                allowed=allowed,
            )
            if chosen is not None:
                return chosen
        return None
