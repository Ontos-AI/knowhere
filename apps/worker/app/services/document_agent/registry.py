"""Tool registry with state-based exposure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.state import DocumentAgentState

ToolHandler = Callable[[ToolContext, dict[str, Any]], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    allowed_states: frozenset[DocumentAgentState]
    handler: ToolHandler

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def catalogue_for_state(self, state: DocumentAgentState) -> list[dict[str, Any]]:
        return [
            tool.to_openai_schema()
            for tool in self._tools.values()
            if state in tool.allowed_states
        ]

    def allowed_names(self, state: DocumentAgentState) -> list[str]:
        return [
            name
            for name, tool in self._tools.items()
            if state in tool.allowed_states
        ]

    def dispatch(
        self,
        name: str,
        ctx: ToolContext,
        args: dict[str, Any],
        state: DocumentAgentState,
    ) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(status="error", error=f"unknown tool: {name}")
        if state not in tool.allowed_states:
            return ToolResult(
                status="state_error",
                payload={"allowed_tools": self.allowed_names(state), "state": state.value},
                error=f"tool {name} is not allowed in state {state.value}",
            )
        return tool.handler(ctx, args)


REGISTRY = ToolRegistry()


def register_tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
    allowed_states: set[DocumentAgentState],
) -> Callable[[ToolHandler], ToolHandler]:
    def _decorator(handler: ToolHandler) -> ToolHandler:
        REGISTRY.register(
            ToolSpec(
                name=name,
                description=description,
                parameters=parameters
                or {"type": "object", "properties": {}, "required": []},
                allowed_states=frozenset(allowed_states),
                handler=handler,
            )
        )
        return handler

    return _decorator
