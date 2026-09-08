# Archived map-nav retrieval route

This directory holds the retired checklist map-nav episode (PLANNER / HARVEST / CONTROL).

It is **not imported** by production code. Lint, typecheck, and pytest skip it.

## Why it was archived

Retrieval now has two live routes:

- `use_agentic is False` → classic map-unit BM25
- otherwise → `agent_explore` (default harness: `cursor_sdk`)

The `RETRIEVAL_AGENTIC_ROUTER=mapnav` switch was removed. Setting that env var has no effect.

Shared scoring used by publication and classic recall was extracted first, to `packages/shared-python/shared/services/retrieval/scoring/`.

## How to restore (manual)

1. Copy these files back to their original paths:
   - `nav/` → `packages/shared-python/shared/services/retrieval/nav/`
   - `nav_config.py`, `nav_snapshot.py`, `nav_bridge.py`, `nav_llm_backend.py` → `packages/shared-python/shared/services/retrieval/`
   - `trace_mapnav.py` → `packages/shared-python/shared/services/retrieval/trace/mapnav.py`
2. Reverse the Phase 1 import moves: leftover map-nav modules expect `shared.services.retrieval.nav.*` for types that now live under `scoring/`.
3. Re-attach a third branch in `execution/routes.py` (`use_agentic is False` → classic, else map-nav or `agent_explore`).
4. Restore the archived tests from `tests/` and stop excluding this directory from pytest.
