"""Agentic evidence retrieval orchestration for Knowhere.

Flow:
  Phase 1: Document selection (discovery + KG LLM select)
  Phase 2: Per-document iterative navigation (navigate_step)
  Phase 3: Render evidence text for downstream agents

Each navigate_step decides action (NAVIGATE/STOP), optional asset tools,
and section selections in a single LLM call. KNOWHERE does not generate
final answers; downstream agents decide whether the evidence is sufficient.
"""
