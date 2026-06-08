"""Prompt projection for agentic section navigation (Collector Agent model)."""
from __future__ import annotations

from typing import Any


def format_nav_trace(
    nav_trace: list[dict[str, Any]],
) -> str:
    """Render the unified navigation trace block.

    Includes compact navigation history. Current collection state is rendered
    separately by the Agent State block.
    """
    if not nav_trace:
        return ""

    lines = ["=== Navigation Trace ==="]
    for entry in nav_trace:
        step = entry.get("step", "?")
        scope = entry.get("scope", "root")
        action = entry.get("action", "?")
        reason = entry.get("reason", "")
        action_display = action
        drill_into = entry.get("drill_into")
        if action == "EXPAND" and drill_into:
            action_display = f'EXPAND "{drill_into}"'
        elif action == "BACK":
            back_to = entry.get("back_to")
            target = f'"{back_to}"' if back_to else "root"
            action_display = f"BACK to {target}"

        lines.append(f"Step {step}: scope={scope} → {action_display}")

        # Show tool usage and results so LLM can avoid repeating searches
        tool_results = entry.get("tool_results", {})
        if tool_results:
            tool_name = tool_results.get("tool", "")
            tool_query = tool_results.get("query", "")
            matched = int(tool_results.get("matched") or 0)
            tool_status = str(tool_results.get("status") or "")
            status = (
                f"found {matched} match(es)"
                if matched
                else f"no matches ({tool_status})"
                if tool_status
                else "no matches"
            )
            lines.append(f'  🔧 {tool_name}("{tool_query}") → {status}')

        # Show what was collected in this step
        step_collected = entry.get("collected", [])
        if step_collected:
            paths_display = ", ".join(f'"{c}"' for c in step_collected)
            lines.append(f"  collected: {paths_display}")

        result_status = entry.get("result_status")
        if result_status and result_status != "ok":
            lines.append(f"  result_status: {result_status}")

        if reason:
            lines.append(f"  reason: {reason}")
        lines.append("")

    lines.append("=== End Trace ===")
    return "\n".join(lines)
