"""Agent tool registry with blackboard-based preconditions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.state import ProfileBlackboard

ToolHandler = Callable[[ToolContext, dict[str, Any]], ToolResult]
Precondition = Callable[[ProfileBlackboard], tuple[bool, str]]


def _always(_blackboard: ProfileBlackboard) -> tuple[bool, str]:
    return True, ""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    preconditions: tuple[Precondition, ...]
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def allowed_names(self, blackboard: ProfileBlackboard) -> list[str]:
        return [
            name
            for name, tool in self._tools.items()
            if self._preconditions_met(tool, blackboard)[0]
        ]

    def _preconditions_met(
        self,
        tool: ToolSpec,
        blackboard: ProfileBlackboard,
    ) -> tuple[bool, str]:
        for check in tool.preconditions:
            ok, reason = check(blackboard)
            if not ok:
                return False, reason
        return True, ""

    def dispatch(
        self,
        name: str,
        ctx: ToolContext,
        args: dict[str, Any],
    ) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(status="error", error=f"unknown tool: {name}")
        ok, reason = self._preconditions_met(tool, ctx.blackboard)
        if not ok:
            return ToolResult(
                status="precondition_unmet",
                payload={
                    "allowed_tools": self.allowed_names(ctx.blackboard),
                    "tool": name,
                    "reason": reason,
                },
                error=reason,
            )
        return tool.handler(ctx, args)


REGISTRY = ToolRegistry()


def register_tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
    preconditions: tuple[Precondition, ...] | None = None,
) -> Callable[[ToolHandler], ToolHandler]:
    def _decorator(handler: ToolHandler) -> ToolHandler:
        REGISTRY.register(
            ToolSpec(
                name=name,
                description=description,
                parameters=parameters
                or {"type": "object", "properties": {}, "required": []},
                preconditions=preconditions or (_always,),
                handler=handler,
            )
        )
        return handler

    return _decorator


def has_page_features(blackboard: ProfileBlackboard) -> tuple[bool, str]:
    return bool(blackboard.page_features), "page_features missing; run bootstrap probe first"


def has_page_labels(blackboard: ProfileBlackboard) -> tuple[bool, str]:
    return bool(blackboard.page_labels), "page_labels missing; run bootstrap classify first"


def has_doc_stats(blackboard: ProfileBlackboard) -> tuple[bool, str]:
    return bool(blackboard.doc_stats), "doc_stats missing; run bootstrap aggregate first"


def has_page_full_text(blackboard: ProfileBlackboard) -> tuple[bool, str]:
    return (
        bool(blackboard.page_full_text_cache),
        "page_full_text_cache missing; run text scan first",
    )


def not_is_scanned(blackboard: ProfileBlackboard) -> tuple[bool, str]:
    profile = blackboard.document_profile
    return (
        profile is not None and not profile.is_scanned,
        "document is scanned or profile is missing; text grep is unavailable",
    )


def has_toc_result(blackboard: ProfileBlackboard) -> tuple[bool, str]:
    return blackboard.toc_result is not None, "toc_result missing; call extract_toc first"


def has_shard_plan(blackboard: ProfileBlackboard) -> tuple[bool, str]:
    return blackboard.shard_plan is not None, "shard_plan missing; call propose_shard first"
