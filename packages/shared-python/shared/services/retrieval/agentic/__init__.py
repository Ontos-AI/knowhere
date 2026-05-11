"""Agentic retrieval orchestration for Knowhere.

Navigate-then-answer loop:
  Phase 1: Document selection (discovery + KG LLM select)
  Phase 2: Per-document iterative navigation (scope_navigate_step)
  Phase 3: attempt_answer → DONE (return answer) or NOT_FOUND → revision

Navigation auto-terminates when the LLM returns empty selections.
After navigation, attempt_answer is called automatically — its result
(answer or NOT_FOUND+reason) drives the revision loop.

All tools are thin wrappers around existing retrieval components — no new
retrieval algorithms, ranking strategies, or prompts are introduced.
"""
