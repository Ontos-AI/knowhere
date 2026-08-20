"""
Agentic retrieval E2E debug runner for the evidence-only flow.

This script uses real DB data and a real LLM. It prints every LLM prompt and
response in full so the routing context is inspectable.

Usage:
  cd apps/worker
  python debug_agentic_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from typing import Any
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../packages/shared-python'))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv
from loguru import logger
from shared.utils.token_estimate import estimate_tokens

load_dotenv(os.path.join(os.path.dirname(__file__), '../.env'))
os.environ.setdefault('LOCAL_DEBUG', '0')
os.environ.setdefault('LLM_MOCK_ENABLED', 'false')

USER_ID = 'debug_local_user'
NAMESPACE = 'default'
TOP_K = 10
CHUNK_SCOPE_DATA_TYPE = {
    'all': 1,
    'text': 2,
    'image': 3,
    'table': 4,
    'text-image': 5,
    'text-table': 6,
    'page': 7,
    'chunk': 8,
}

captured_interactions: list[dict[str, Any]] = []


@contextmanager
def temporary_env(overrides: dict[str, str] | None):
    """Apply per-test env overrides and restore them afterwards."""
    overrides = overrides or {}
    old_values = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _print_box(title: str, body: Any) -> None:
    line = '=' * 100
    print(f'\n{line}\n{title}\n{line}')
    print(str(body))
    print(line)


def _decision_stage(kind: str) -> tuple[str, str]:
    return {
        'kg_document_select': (
            'Phase 1B: KG Document Select',
            'LLM reads the KB inventory and chooses candidate documents. Bottom discovery is merged separately so missed high-recall docs are still protected.',
        ),
        'navigate': (
            'Phase 2A: Navigate (unified)',
            'LLM decides action (NAVIGATE/STOP), optional asset tools (SEARCH_IMAGES/SEARCH_TABLES), and section selections in a single call.',
        ),
        'asset_filter': (
            'Phase 2A-1: Asset Filter',
            'LLM filters asset candidates (images/tables) by semantic relevance to the search query. Only matching assets are added to pending evidence.',
        ),
        'workflow_planner': (
            'Phase 0: Workflow Planner',
            'Thinking-model planner decides whether to decompose the query into multiple retrieval sub-steps or pass through as a single step.',
        ),
    }.get(kind, ('Unknown Phase', 'Unclassified LLM call.'))


def _fence(text: Any, lang: str = 'text') -> str:
    body = str(text).replace('```', '``\\`')
    return f'```{lang}\n{body}\n```\n'


def _extract_resource_status(prompt: str) -> dict[str, str]:
    """Extract budget lines from LLM prompts for compact trace summaries."""
    status: dict[str, str] = {}
    for raw_line in str(prompt).splitlines():
        line = raw_line.strip()
        if line.startswith('Planning Budget:'):
            status['planning'] = line.removeprefix('Planning Budget:').strip()
        elif line.startswith('Context Budget:'):
            status['context'] = line.removeprefix('Context Budget:').strip()
        elif line.startswith('Planning budget:'):
            status['planning'] = line.removeprefix('Planning budget:').strip()
        elif line.startswith('Context budget:'):
            status['context'] = line.removeprefix('Context budget:').strip()
    return status


def _extract_context_projection(prompt: str) -> dict[str, int | str]:
    """Estimate context budget as the debugger sees this exact prompt."""
    resource_status = _extract_resource_status(prompt)
    projection: dict[str, int | str] = {
        'prompt_tokens_estimate': estimate_tokens(prompt),
    }
    context = resource_status.get('context', '')
    match = re.search(r'(\d+)\s*/\s*(\d+)\s+remaining', context)
    if match:
        remaining_in_prompt = int(match.group(1))
        capacity = int(match.group(2))
        remaining_pct = 0 if capacity <= 0 else max(
            0,
            min(100, round(remaining_in_prompt * 100 / capacity)),
        )
        projection.update({
            'context_remaining_in_prompt': remaining_in_prompt,
            'context_capacity': capacity,
            'context_used_pct_in_prompt': 100 - remaining_pct,
            'context_remaining_pct_in_prompt': remaining_pct,
        })
    return projection


def _charge_pool_for_kind(kind: str) -> str:
    return {
        'kg_document_select': 'bootstrap',
        'navigate': 'planning',
        'asset_filter': 'planning',
        'workflow_planner': 'bootstrap',
    }.get(kind, 'unknown')


def make_verbose_llm(real_llm_fn):
    counter = {'n': 0}

    async def verbose_llm(prompt) -> str:
        counter['n'] += 1
        n = counter['n']
        if '=== Document Corpus Overview ===' in prompt:
            kind = 'kg_document_select'
        elif '=== Rules ===' in prompt and '=== Actionable Observation ===' in prompt:
            kind = 'navigate'
        elif 'retrieval workflow planner' in prompt.lower():
            kind = 'workflow_planner'
        elif 'You are an asset relevance filter' in prompt:
            kind = 'asset_filter'
        else:
            kind = 'unknown'
        _print_box(f'LLM PROMPT #{n} [{kind}] chars={len(prompt)}', prompt)
        context_projection = _extract_context_projection(str(prompt))
        charge_pool = _charge_pool_for_kind(kind)
        logger.info(
            'LLM PROMPT #{} [{}] pool={} token_estimate={}{}',
            n,
            kind,
            charge_pool,
            context_projection.get('prompt_tokens_estimate'),
            (
                ' context_in_prompt='
                f"{context_projection.get('context_remaining_in_prompt')}/"
                f"{context_projection.get('context_capacity')}"
            )
            if 'context_remaining_in_prompt' in context_projection else '',
        )
        t0 = time.monotonic()
        response = await real_llm_fn(prompt)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _print_box(
            f'LLM RESPONSE #{n} [{kind}] elapsed={elapsed_ms}ms chars={len(response)}',
            response,
        )
        captured_interactions.append({
            'call_index': n,
            'kind': kind,
            'charge_pool': charge_pool,
            'prompt_chars': len(prompt),
            'prompt': prompt,
            'resource_status': _extract_resource_status(str(prompt)),
            'context_projection': context_projection,
            'response_chars': len(response),
            'response': response,
            'latency_ms': elapsed_ms,
        })
        return response

    return verbose_llm


async def phase1_contract() -> dict[str, dict[str, Any]]:
    from sqlalchemy import text
    from shared.core.database import get_db_context

    logger.info('\n' + '█' * 80)
    logger.info('Phase 1: DB contract check for current design')
    logger.info('█' * 80)

    docs: dict[str, dict[str, Any]] = {}
    async with get_db_context() as db:
        rows = (await db.execute(text(
            """
            SELECT d.document_id, d.source_file_name, d.current_job_result_id,
                   count(DISTINCT dc.id) AS chunk_count,
                   count(DISTINCT ds.section_id) AS section_count
            FROM documents d
            LEFT JOIN document_chunks dc
              ON dc.document_id = d.document_id
             AND dc.job_result_id = d.current_job_result_id
            LEFT JOIN document_sections ds
              ON ds.document_id = d.document_id
             AND ds.job_result_id = d.current_job_result_id
            WHERE d.user_id=:u AND d.namespace=:n AND d.status='active'
            GROUP BY d.document_id, d.source_file_name, d.current_job_result_id
            ORDER BY section_count DESC, chunk_count DESC
            """
        ), {'u': USER_ID, 'n': NAMESPACE})).all()

        for doc_id, fname, job_result_id, chunk_count, section_count in rows:
            props = (await db.execute(text(
                """
                SELECT properties
                FROM graph_nodes
                WHERE owner_document_id=:d AND node_kind='document'
                LIMIT 1
                """
            ), {'d': doc_id})).scalar()
            has_nav_sections = isinstance(props, dict) and 'nav_sections' in props
            logger.info(
                'doc={} name={} chunks={} sections={} graph_has_nav_sections={}',
                doc_id,
                fname,
                chunk_count,
                section_count,
                has_nav_sections,
            )
            docs[doc_id] = {
                'fname': fname,
                'job_result_id': job_result_id,
                'chunk_count': int(chunk_count or 0),
                'section_count': int(section_count or 0),
                'graph_has_nav_sections': has_nav_sections,
            }

    return docs


async def phase2_scope_candidates(docs: dict[str, dict[str, Any]]) -> str | None:
    from shared.core.database import get_db_context
    from shared.services.retrieval.agentic.navigation.section_tree import load_child_sections

    logger.info('\n' + '█' * 80)
    logger.info('Phase 2: _load_child_sections root L1/L2 candidates')
    logger.info('█' * 80)

    target_doc_id = next(
        (
            doc_id for doc_id, info in docs.items()
            if info['section_count'] > 0 and info['job_result_id']
        ),
        None,
    )
    if not target_doc_id:
        logger.warning('No document with sections found')
        return None

    info = docs[target_doc_id]
    async with get_db_context() as db:
        items = await load_child_sections(db, target_doc_id, info['job_result_id'], None)
    logger.info('selected_doc={} name={} items={}', target_doc_id, info['fname'], len(items))
    for item in items[:30]:
        logger.info(
            '  L{} text={} image={} table={} path="{}"',
            item.get('level'),
            item.get('chunk_count'),
            item.get('image_count'),
            item.get('table_count'),
            item.get('path'),
        )
    if len(items) > 30:
        logger.info('  ... and {} more', len(items) - 30)
    return target_doc_id


async def run_test(
    query: str,
    label: str,
    env_overrides: dict[str, str] | None = None,
    expected_decision: str = '',
) -> dict[str, Any]:
    """Unified test runner — all queries go through WorkflowOrchestrator.

    Mirrors the production agentic path (use_agentic=True) which routes
    through WorkflowOrchestrator (planner → per-step RetrievalAgent).
    """
    from shared.core.database import get_db_context
    from shared.services.retrieval.llm_adapter import create_retrieval_llm_fn
    from shared.services.retrieval.workflow.orchestrator import WorkflowOrchestrator
    from shared.services.retrieval.agentic.navigation.section_tree import (
        load_child_sections,
    )

    logger.info('\n' + '█' * 80)
    logger.info(f'TEST [{label}] | QUERY: {query}')
    logger.info('█' * 80)

    captured_interactions.clear()

    real_llm = create_retrieval_llm_fn()
    if real_llm is None:
        logger.error('LLM is not configured')
        return {'error': 'no_llm'}
    llm_fn = make_verbose_llm(real_llm)

    import shared.services.retrieval.agentic.navigation.section_tree as nav_mod
    import shared.services.retrieval.agentic.navigation.tools as nav_tools_mod
    orig_load_child_sections = nav_mod.load_child_sections
    orig_tools_load_child_sections = nav_tools_mod.load_child_sections

    async def verbose_load_child_sections(
        db,
        document_id,
        job_result_id,
        scope_path=None,
        exclude_paths=None,
        limit_depth=True,
        section_rows=None,
    ):
        items = await load_child_sections(
            db,
            document_id,
            job_result_id,
            scope_path,
            exclude_paths=exclude_paths,
            limit_depth=limit_depth,
            section_rows=section_rows,
        )
        _print_box(
            f'SCOPE CANDIDATES doc={document_id} scope={scope_path or "root"} '
            f'count={len(items)} exclude_paths={exclude_paths}',
            '\n'.join(
                f'- L{item.get("level")} text={item.get("chunk_count")} '
                f'image={item.get("image_count")} table={item.get("table_count")} '
                f'is_leaf={item.get("is_leaf")} '
                f'path="{item.get("path")}"\n'
                f'  summary={(item.get("summary") or "")[:240]}'
                for item in items
            ),
        )
        return items

    # Patch on both modules since tools.py caches the direct import
    nav_mod.load_child_sections = verbose_load_child_sections
    nav_tools_mod.load_child_sections = verbose_load_child_sections

    try:
        t0 = time.monotonic()
        with temporary_env(env_overrides):
            async with get_db_context() as db:
                result = await WorkflowOrchestrator().run(
                    db,
                    user_id=USER_ID,
                    namespace=NAMESPACE,
                    query=query,
                    top_k=TOP_K,
                    exclude_document_ids=[],
                    exclude_sections=[],
                    llm_fn=llm_fn,
                )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
    finally:
        # Restore originals for next test
        nav_mod.load_child_sections = orig_load_child_sections
        nav_tools_mod.load_child_sections = orig_tools_load_child_sections

    # Extract action list from captured interactions
    actions = [c['kind'] for c in captured_interactions]
    workflow_steps = [step.to_api_dict() for step in result.steps]
    budget_accounting = _build_budget_accounting(
        interactions=captured_interactions,
        workflow_steps=workflow_steps,
    )
    key_decisions = _build_key_decisions(
        result=result,
        workflow_steps=workflow_steps,
        expected_decision=expected_decision,
        env_overrides=env_overrides or {},
        budget_accounting=budget_accounting,
    )

    return {
        'label': label,
        'query': query,
        'expected_decision': expected_decision,
        'env_overrides': env_overrides or {},
        'key_decisions': key_decisions,
        'budget_accounting': budget_accounting,
        'router_used': result.router_used,
        'stop_reason': '',
        'total_ms': elapsed_ms,
        'actions': actions,
        'evidence_text_chars': sum(len(step.evidence_text or '') for step in result.steps),
        'evidence_text': '\n\n'.join(step.evidence_text or '' for step in result.steps),
        'answer_text_chars': len(result.answer_text),
        'answer_text': result.answer_text,
        'referenced_chunks_count': len(result.referenced_chunks),
        'referenced_chunks': result.referenced_chunks,
        'budget_snapshot': result.wallet_snapshot,
        'workflow_plan': result.plan.to_dict() if result.plan else None,
        'workflow_steps': workflow_steps,
        'wallet_snapshot': result.wallet_snapshot,
        'planner_snapshot': result.planner_snapshot,
        'llm_interactions': len(captured_interactions),
        'llm_interaction_details': [
            {
                'call_index': c['call_index'],
                'kind': c['kind'],
                'charge_pool': c.get('charge_pool', 'unknown'),
                'latency_ms': c['latency_ms'],
                'prompt_chars': c['prompt_chars'],
                'response_chars': c['response_chars'],
                'prompt': c['prompt'],
                'resource_status': c.get('resource_status', {}),
                'context_projection': c.get('context_projection', {}),
                'response': c['response'],
            }
            for c in captured_interactions
        ],
    }


async def run_single_query(
    *,
    query: str,
    label: str,
    top_k: int,
    data_type: int,
) -> dict[str, Any]:
    """Run one query through the public production retrieval entry."""
    from shared.core.database import get_db_context
    from shared.services.retrieval.app_service import run_retrieval_query

    _DATA_TYPE_MAP: dict[int, set[str] | None] = {
        1: None, 2: {"text"}, 3: {"image"}, 4: {"table"},
        5: {"text", "image"}, 6: {"text", "table"},
        7: {"page"}, 8: {"text", "image", "table"},
    }
    chunk_types = _DATA_TYPE_MAP.get(data_type)

    t0 = time.monotonic()
    async with get_db_context() as db:
        result = await run_retrieval_query(
            db=db,
            user_id=USER_ID,
            namespace=NAMESPACE,
            query=query,
            top_k=top_k,
            exclude_document_ids=[],
            exclude_sections=[],
            chunk_types=chunk_types,
            # --query mode should exercise the production agentic path; omitting
            # this falls through to classic_topk and skips WorkflowOrchestrator.
            use_agentic=True,
        )
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return {
        'label': label,
        'query': query,
        'top_k': top_k,
        'data_type': data_type,
        'namespace': NAMESPACE,
        'total_ms': elapsed_ms,
        'result': result,
    }


def _asset_url_values(item: dict[str, Any]) -> list[str]:
    if item.get('asset_url'):
        return [str(item['asset_url'])]
    return []


def _render_single_query_report(report: dict[str, Any]) -> str:
    result = report.get('result') or {}
    refs = result.get('referenced_chunks') or []
    rows = result.get('results') or []
    evidence = result.get('evidence_text') or ''
    md = [
        '# Retrieval Debug Trace\n',
        f"Query: `{report.get('query', '')}`\n",
        f"Label: `{report.get('label', '')}`\n",
        f"Namespace: `{report.get('namespace', '')}`\n",
        f"data_type: `{report.get('data_type')}`\n",
        (
            f"Run summary: router=`{result.get('router_used')}`, "
            f"stop_reason=`{result.get('stop_reason', '')}`, "
            f"elapsed={report.get('total_ms')}ms, "
            f"evidence={len(evidence)} chars, refs={len(refs)}, results={len(rows)}.\n"
        ),
        '\n## Referenced Chunks\n',
    ]
    if refs:
        for i, ref in enumerate(refs, 1):
            urls = _asset_url_values(ref)
            md.append(
                f"{i}. type=`{ref.get('chunk_type') or ref.get('type') or ''}`, "
                f"section=`{ref.get('section_path', '')}`, "
                f"file_path=`{ref.get('file_path', '')}`, asset_url_count={len(urls)}\n"
            )
            for url in urls:
                md.append(f"   - {url}\n")
    else:
        md.append('No referenced chunks.\n')

    md.append('\n## Results\n')
    if rows:
        for i, row in enumerate(rows, 1):
            urls = _asset_url_values(row)
            md.append(
                f"{i}. type=`{row.get('chunk_type') or row.get('type') or ''}`, "
                f"section=`{row.get('section_path', '')}`, "
                f"score=`{row.get('score', '')}`, asset_url_count={len(urls)}\n"
            )
            for url in urls:
                md.append(f"   - {url}\n")
    else:
        md.append('No result rows.\n')

    md.append(f"\n## Evidence Text\n\n```text\n{evidence}\n```\n")
    return ''.join(md)


def _budget_pool_line(snapshot: dict[str, Any] | None, pool_name: str) -> str:
    if not isinstance(snapshot, dict):
        return 'n/a'
    pool = snapshot.get(pool_name)
    if not isinstance(pool, dict):
        return 'n/a'
    capacity = pool.get('capacity', '?')
    remaining = pool.get('remaining', '?')
    try:
        capacity_int = int(capacity)
        remaining_int = int(remaining)
        remaining_pct: int | str = 0 if capacity_int <= 0 else max(
            0,
            min(100, round(remaining_int * 100 / capacity_int)),
        )
    except (TypeError, ValueError):
        remaining_pct = '?'
    return (
        f"{pool.get('status', '?')} "
        f"used={pool.get('used_pct', '?')}% "
        f"remaining={remaining_pct}% ({remaining}/{capacity})"
    )


def _format_trimmed_path(item: Any, index: int) -> str:
    if not isinstance(item, dict):
        return f"{index}. path=`{item}`"
    return (
        f"{index}. doc=`{item.get('document_name') or item.get('document_id', '')}`, "
        f"path=`{item.get('path', '')}`, "
        f"confidence={item.get('confidence_score', '?')}, "
        f"discovery={item.get('discovery_score', '?')}, "
        f"importance={item.get('importance_score', '?')}, "
        f"tokens~={item.get('token_estimate', '?')}"
    )


def _compact_json(value: Any) -> str:
    if not value:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(',', ': '))


def _budget_line(snapshot: dict[str, Any] | None) -> str:
    if not isinstance(snapshot, dict):
        return ""
    parts: list[str] = []
    for pool_name in ('bootstrap', 'planning', 'context'):
        pool = snapshot.get(pool_name)
        if isinstance(pool, dict):
            parts.append(
                f"{pool_name}={pool.get('status', '?')} "
                f"{pool.get('used_pct', 0)}% "
                f"({pool.get('remaining', 0)}/{pool.get('capacity', 0)})"
            )
    return '; '.join(parts)


def _trace_observation_summary(observation: dict[str, Any]) -> str:
    if not isinstance(observation, dict) or not observation:
        return ""
    summary: dict[str, Any] = {}
    for key in (
        'candidate_count',
        'visible_count',
        'total_steps',
        'collected_count',
        'guard_triggered',
        'exit_reason',
        'query',
    ):
        if key in observation:
            summary[key] = observation[key]
    if 'exclude_set' in observation:
        exclude_set = observation.get('exclude_set') or []
        summary['exclude_set_count'] = len(exclude_set)
    if not summary:
        return ""
    return _compact_json(summary)


def _append_trace_collected(
    md: list[str],
    collected: Any,
    *,
    label: str = 'Collected',
    limit: int = 20,
) -> None:
    if not isinstance(collected, list) or not collected:
        return
    md.append(f"  - {label}: {len(collected)}\n")
    for item in collected[:limit]:
        if isinstance(item, dict):
            path = item.get('path', '')
            confidence = item.get('confidence')
            suffix = f" confidence={confidence}" if confidence is not None else ""
            hydrate_mode = item.get('hydrate_mode')
            if hydrate_mode:
                suffix += f" mode={hydrate_mode}"
            md.append(f"    - `{path}`{suffix}\n")
        else:
            md.append(f"    - `{item}`\n")
    if len(collected) > limit:
        md.append(f"    - (+{len(collected) - limit} more)\n")


def _pool_remaining(snapshot: dict[str, Any] | None, pool_name: str) -> int | None:
    if not isinstance(snapshot, dict):
        return None
    pool = snapshot.get(pool_name)
    if not isinstance(pool, dict):
        return None
    remaining = pool.get('remaining')
    if remaining is None:
        return None
    try:
        return int(remaining)
    except (TypeError, ValueError):
        return None


def _build_budget_accounting(
    *,
    interactions: list[dict[str, Any]],
    workflow_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    pool_estimates = {'bootstrap': 0, 'planning': 0, 'context': 0, 'unknown': 0}
    rows = []
    for interaction in interactions:
        pool = str(interaction.get('charge_pool') or 'unknown')
        if pool not in pool_estimates:
            pool = 'unknown'
        estimate = int(
            (interaction.get('context_projection') or {}).get('prompt_tokens_estimate')
            or 0
        )
        before = pool_estimates[pool]
        pool_estimates[pool] = before + estimate
        rows.append({
            'call_index': interaction.get('call_index'),
            'kind': interaction.get('kind'),
            'pool': pool,
            'prompt_tokens_estimate': estimate,
            'pool_estimate_before': before,
            'pool_estimate_after': pool_estimates[pool],
        })

    final_context_remaining = next(
        (
            _pool_remaining(step.get('budget_snapshot'), 'context')
            for step in workflow_steps
            if step.get('budget_snapshot')
        ),
        None,
    )
    evidence_tokens = max(
        estimate_tokens(step.get('evidence_text') or '')
        for step in workflow_steps
    ) if workflow_steps else 0

    return {
        'rows': rows,
        'pool_estimates': pool_estimates,
        'evidence_tokens_estimate': evidence_tokens,
        'final_context_remaining': final_context_remaining,
    }


def _build_key_decisions(
    *,
    result,
    workflow_steps: list[dict[str, Any]],
    expected_decision: str,
    env_overrides: dict[str, str],
    budget_accounting: dict[str, Any],
) -> list[str]:
    planner_snapshot = result.planner_snapshot or {}
    wallet_snapshot = result.wallet_snapshot or {}
    step_statuses = [str(step.get('status') or '') for step in workflow_steps]
    step_stop_reasons = [str(step.get('stop_reason') or '') for step in workflow_steps]
    step_failure_reasons = [
        str(step.get('failure_reason') or '')
        for step in workflow_steps
        if step.get('failure_reason')
    ]

    decisions = [
        (
            'Planner inventory: '
            f"{planner_snapshot.get('total_docs', 0)} docs / "
            f"{planner_snapshot.get('total_chunks', 0)} chunks"
        ),
        (
            'Workflow wallet: '
            f"total={wallet_snapshot.get('total', 'n/a')} "
            f"remaining={wallet_snapshot.get('remaining', 'n/a')} "
            f"allocated={wallet_snapshot.get('allocated', 'n/a')}"
        ),
        f"Step statuses: {', '.join(step_statuses) or 'none'}",
        f"Step stop reasons: {', '.join(step_stop_reasons) or 'none'}",
    ]
    if env_overrides:
        decisions.append(
            'Env overrides: '
            + ', '.join(f'{key}={value}' for key, value in sorted(env_overrides.items()))
        )
    for step in workflow_steps:
        snap = step.get('budget_snapshot') or {}
        trimmed_paths = snap.get('trimmed_paths') or []
        decisions.append(
            f"Step {step.get('step_id')} budget: "
            f"planning={_budget_pool_line(snap, 'planning')}; "
            f"context={_budget_pool_line(snap, 'context')}; "
            f"inventory={snap.get('total_docs', 0)} docs/{snap.get('total_chunks', 0)} chunks"
        )
        if trimmed_paths:
            preview = '; '.join(
                str(item.get('path') or item)[:160]
                for item in trimmed_paths[:3]
            )
            decisions.append(
                f"Trimmed paths: {len(trimmed_paths)} section(s) removed before answer. "
                f"Preview: {preview}"
            )
    if step_failure_reasons:
        decisions.append('Failure reason propagated: ' + '；'.join(step_failure_reasons))
    decisions.append(
        'Evidence-only contract: final answer is not generated inside KNOWHERE.'
    )

    if expected_decision == 'budget_stop':
        decisions.append(
            'Decision check: PASS' if any(status == 'budget_stop' for status in step_statuses)
            else 'Decision check: FAIL (expected budget_stop)'
        )
    elif expected_decision == 'not_found':
        decisions.append(
            'Decision check: PASS' if any(status == 'not_found' for status in step_statuses)
            else 'Decision check: FAIL (expected not_found)'
        )
    return decisions



def _render_md_report(all_reports: list[dict[str, Any]]) -> str:
    """Render a readable algorithm trace report for the latest agentic flow."""
    md = [
        '# Agentic Retrieval E2E Trace Report\n',
        'This report is generated from real DB data and real LLM calls. It follows the current evidence-only algorithm flow: bottom discovery, KG document selection, per-document navigation, discovery merge, and evidence rendering.\n',
        'The debug runner writes only this Markdown report; no JSON artifact is emitted.\n',
    ]

    for r in all_reports:
        md.append(f"\n## {r['label']}\n")
        md.append(f"Query: `{r['query']}`\n")
        md.append(
            f"Run summary: router=`{r['router_used']}`, stop_reason=`{r.get('stop_reason', '')}`, "
            f"elapsed={r['total_ms']}ms, "
            f"LLM calls={r.get('llm_interactions', 0)}, evidence={r.get('evidence_text_chars', 0)} chars, "
            f"answer={r.get('answer_text_chars', 0)} chars, referenced_chunks={r.get('referenced_chunks_count', 0)}.\n"
        )
        md.append(f"Action sequence: `{' -> '.join(r['actions'])}`\n\n")

        key_decisions = r.get('key_decisions') or []
        if key_decisions:
            md.append('### Key Decision Checks\n')
            for item in key_decisions:
                md.append(f"- {item}\n")
            md.append('\n')

        budget_accounting = r.get('budget_accounting') or {}
        accounting_rows = budget_accounting.get('rows') or []
        if accounting_rows:
            md.append('### Budget Accounting\n')
            for row in accounting_rows:
                md.append(
                    f"- Call {row.get('call_index')} `{row.get('kind')}` "
                    f"pool=`{row.get('pool')}` prompt_est={row.get('prompt_tokens_estimate')} "
                    f"pool_estimate={row.get('pool_estimate_before')}→{row.get('pool_estimate_after')}\n"
                )
            md.append(
                f"- Evidence tokens estimate: {budget_accounting.get('evidence_tokens_estimate')} tokens; "
                f"final context remaining: {budget_accounting.get('final_context_remaining')} tokens; "
                "no answer prompt is built inside KNOWHERE.\n\n"
            )

        if r.get('workflow_plan'):
            md.append('### Workflow Plan\n')
            md.append(_fence(r['workflow_plan'], 'json'))
            md.append('### Workflow Steps\n')
            for step in r.get('workflow_steps', []):
                md.append(
                    f"- `{step.get('step_id')}` kind=`{step.get('step_kind')}` "
                    f"status=`{step.get('status')}` role=`{step.get('output_role')}` "
                    f"depends_on=`{step.get('depends_on')}` refs={len(step.get('referenced_chunks') or [])} "
                    f"stop_reason=`{step.get('stop_reason')}`\n"
                )
                if step.get('failure_reason'):
                    md.append(f"  failure_reason: `{step.get('failure_reason')}`\n")
                if step.get('answer_text'):
                    md.append(_fence(step.get('answer_text')))
            if r.get('wallet_snapshot'):
                md.append('### Wallet Snapshot\n')
                md.append(_fence(r['wallet_snapshot'], 'json'))
            if r.get('planner_snapshot'):
                md.append('### Planner Snapshot\n')
                md.append(_fence(r['planner_snapshot'], 'json'))

        # Decision Route Summary
        md.append('### Decision Route\n')
        interactions = r.get('llm_interaction_details', [])
        for interaction in interactions:
            kind = interaction['kind']
            idx = interaction['call_index']
            latency = interaction['latency_ms']
            resp = interaction.get('response', '')
            resource_status = interaction.get('resource_status') or {}
            context_projection = interaction.get('context_projection') or {}
            stage, _purpose = _decision_stage(kind)
            resp_preview = resp[:200].replace('\n', ' ').strip()
            budget_bits = []
            prompt_estimate = context_projection.get('prompt_tokens_estimate')
            charge_pool = interaction.get('charge_pool') or 'unknown'
            if prompt_estimate is not None:
                budget_bits.append(f"{charge_pool}_prompt_est={prompt_estimate}")
            if resource_status.get('planning'):
                budget_bits.append(f"planning={resource_status.get('planning')}")
            if resource_status.get('context'):
                budget_bits.append(f"context={resource_status.get('context')}")
            if 'context_remaining_in_prompt' in context_projection:
                budget_bits.append(
                    'context_prompt='
                    f"{context_projection.get('context_remaining_in_prompt')}/"
                    f"{context_projection.get('context_capacity')} "
                    f"used={context_projection.get('context_used_pct_in_prompt')}% "
                    f"remaining={context_projection.get('context_remaining_pct_in_prompt')}%"
                )
            budget_suffix = f" budget: {'; '.join(budget_bits)}" if budget_bits else ''
            md.append(
                f"{idx}. **{stage}** ({latency}ms){budget_suffix} — `{resp_preview}`\n"
            )
        md.append('\n')

        md.append('### Algorithm Trace\n')
        md.append('Phase 1A Bottom Discovery always runs before these LLM calls. It performs high-recall lexical discovery and contributes document/path hints to later phases.\n\n')

        for interaction in interactions:
            kind = interaction['kind']
            idx = interaction['call_index']
            latency = interaction['latency_ms']
            stage, purpose = _decision_stage(kind)

            md.append(f"#### Step {idx}: {stage}\n")
            md.append(f"Kind: `{kind}`. Latency: {latency}ms. Prompt chars: {interaction['prompt_chars']}. Response chars: {interaction['response_chars']}.\n")
            resource_status = interaction.get('resource_status') or {}
            if resource_status:
                md.append(
                    'Budget: '
                    f"planning=`{resource_status.get('planning', '?')}`, "
                    f"context=`{resource_status.get('context', '?')}`.\n"
                )
            context_projection = interaction.get('context_projection') or {}
            if context_projection:
                md.append(
                    'Debug budget: '
                    f"prompt_tokens_estimate=`{context_projection.get('prompt_tokens_estimate', '?')}`"
                )
                if 'context_remaining_in_prompt' in context_projection:
                    md.append(
                        ', '
                        f"context_remaining_in_prompt=`{context_projection.get('context_remaining_in_prompt')}/"
                        f"{context_projection.get('context_capacity')}` "
                        f"({context_projection.get('context_used_pct_in_prompt')}% used, "
                        f"{context_projection.get('context_remaining_pct_in_prompt')}% remaining)"
                    )
                md.append('.\n')
            md.append(f"Purpose: {purpose}\n")
            md.append('<details><summary>Full Prompt</summary>\n\n')
            md.append(_fence(interaction['prompt']))
            md.append('\n</details>\n\n')
            md.append('LLM response:\n')
            md.append(_fence(interaction['response'], 'json'))
            md.append('\n')

        md.append('### Answer Contract\n')
        md.append('`answer_text` is intentionally empty. Downstream agents synthesize answers from `evidence_text`.\n\n')

        evidence_text = r.get('evidence_text', '')
        md.append('### Rendered Evidence\n')
        if evidence_text:
            md.append(f'<details><summary>Full evidence_text ({len(evidence_text)} chars)</summary>\n\n')
            md.append(_fence(evidence_text))
            md.append('\n</details>\n\n')
        else:
            md.append('No rendered evidence collected.\n\n')

        refs = r.get('referenced_chunks', [])
        md.append('### Referenced Chunks\n')
        if refs:
            for i, ref in enumerate(refs):
                md.append(
                    f"{i + 1}. type=`{ref.get('chunk_type', '')}`, "
                    f"section=`{ref.get('section_path', '')}`, "
                    f"file_path=`{ref.get('file_path', '')}`\n"
                )
            md.append("\n")
        else:
            md.append('No referenced chunks.\n\n')

        # Decision Trace — navigation decisions per step
        all_decision_trace: list[dict] = []
        for step in r.get('workflow_steps', []):
            dt = step.get('decision_trace') or []
            for entry in dt:
                entry['_step_id'] = step.get('step_id', '?')
            all_decision_trace.extend(dt)
        if all_decision_trace:
            md.append('### Decision Trace\n')
            md.append('Navigation decisions made during agentic retrieval. '
                       'Each row follows observe -> decide -> result.\n\n')
            for entry in all_decision_trace:
                phase = entry.get('phase', '?')
                agent = entry.get('agent', '?')
                doc = entry.get('document') or ''
                step_id = entry.get('_step_id', '?')
                trace_index = entry.get('step_index', '?')
                parent_index = entry.get('parent_step_index')
                scope = entry.get('scope') or 'root'
                observation = entry.get('observation') or {}
                decision = entry.get('decision') or {}
                result = entry.get('result') or {}
                action = decision.get('action', '?')
                args = decision.get('args') or {}
                reason = decision.get('reason') or ''
                status = result.get('status', '?')

                md.append(
                    f"- **[{step_id}] #{trace_index} {agent}.{phase}** "
                    f"doc=`{doc}` scope=`{scope}` action=`{action}` "
                    f"status=`{status}`"
                )
                if parent_index is not None:
                    md.append(f" parent=`#{parent_index}`")
                if reason:
                    md.append(f" — {reason}")
                md.append('\n')

                if args:
                    md.append(f"  - Args: `{_compact_json(args)}`\n")

                observation_summary = _trace_observation_summary(observation)
                if observation_summary:
                    md.append(f"  - Observation: `{observation_summary}`\n")

                _append_trace_collected(md, result.get('collected'))

                excluded = result.get('excluded_hints') or []
                if excluded:
                    md.append(f"  - Excluded hints: {len(excluded)}\n")
                    for hint in excluded[:5]:
                        md.append(
                            f"    - `{hint.get('path', '')}` "
                            f"(covered by `{hint.get('covered_by', '')})`\n"
                        )
                    if len(excluded) > 5:
                        md.append(f"    - (+{len(excluded) - 5} more)\n")

                for key in (
                    'hydrated_count',
                    'matched_assets',
                    'tool_status',
                    'sub_agent_assessment',
                    'note',
                    'new_scope',
                    'error',
                ):
                    value = result.get(key)
                    if value not in (None, '', [], {}):
                        md.append(f"  - {key}: `{value}`\n")

                budget_summary = _budget_line(entry.get('budget'))
                if budget_summary:
                    md.append(f"  - Budget: {budget_summary}\n")

            md.append('\n')

            md.append('\n')

        # Final Budget Snapshot
        budget = r.get('budget_snapshot')
        if budget:
            md.append('### Final Budget Snapshot\n')
            for pool_name in ('bootstrap', 'planning', 'context'):
                pool = budget.get(pool_name)
                if isinstance(pool, dict):
                    md.append(
                        f"- **{pool_name}**: {pool.get('status', '?')} "
                        f"({pool.get('used_pct', 0)}% used, "
                        f"remaining={pool.get('remaining', 0)}/{pool.get('capacity', 0)})\n"
                    )
            md.append(
                f"- **Coverage**: {budget.get('explored_chunks', 0)}/{budget.get('total_chunks', 0)} chunks, "
                f"{budget.get('explored_docs', 0)}/{budget.get('total_docs', 0)} docs\n"
            )
            trimmed = budget.get('trimmed_paths', [])
            if trimmed:
                md.append(f"- **Trimmed paths**: {len(trimmed)} sections removed for budget\n")
                for i, item in enumerate(trimmed, 1):
                    md.append(f"  - {_format_trimmed_path(item, i)}\n")
            md.append('\n')

        md.append("\n---\n")

    return '\n'.join(md)


async def main() -> None:
    from datetime import datetime

    # Enable verbose logging to see full LLM prompts and responses
    os.environ['RETRIEVAL_AGENTIC_VERBOSE'] = 'true'
    os.environ['RETRIEVAL_AGENTIC_TRACE_ENABLED'] = 'false'
    os.environ['RETRIEVAL_DECOMPOSITION_MAX_STEPS'] = '5'

    tests = [
        # ── Case 1: Original failing case (NAVIGATE → STOP → empty [DrillDown])
        # LLM navigates into "四、市场分析" then STOPs with no_relevant_child.
        # Before fix: produces empty `▸ 四、 市场分析 [DrillDown]`
        # After fix: child removed, evidence shows full document outline.
        {
            'query': '安全大模型市场规模预测 2024 2025',
            'label': 'T1_Market_Size_NAVIGATE_STOP',
            'expected_decision': '',
            'env_overrides': {
                'RETRIEVAL_WALLET_TOTAL_BUDGET': '200000',
                'RETRIEVAL_WALLET_PER_RETRIEVE_STEP_BUDGET': '40000',
                'RETRIEVAL_AGENTIC_BOOTSTRAP_BUDGET': '2000',
                'RETRIEVAL_AGENTIC_PLANNING_RATIO': '0.5',
                'RETRIEVAL_AGENTIC_LATENCY_BUDGET_MS': '30000',
            },
        },
        # ── Case 2: Deep hierarchy drill — should NAVIGATE → NAVIGATE → leaf hydration
        # Targets a specific vendor inside "四、市场分析 / (二) 国内..." → should drill
        # to L3 leaf and hydrate actual content.
        {
            'query': '深信服安全大模型的技术方案和部署形态',
            'label': 'T2_Deep_Drill_Vendor',
            'expected_decision': '',
            'env_overrides': {
                'RETRIEVAL_WALLET_TOTAL_BUDGET': '200000',
                'RETRIEVAL_WALLET_PER_RETRIEVE_STEP_BUDGET': '40000',
                'RETRIEVAL_AGENTIC_BOOTSTRAP_BUDGET': '2000',
                'RETRIEVAL_AGENTIC_PLANNING_RATIO': '0.5',
                'RETRIEVAL_AGENTIC_LATENCY_BUDGET_MS': '30000',
            },
        },
        # ── Case 3: Leaf-level match — query matches a specific L1 leaf section
        # "法律声明" is a [Leaf] L1 node. Should SELECT directly, no drill-down needed.
        {
            'query': '这份安全报告的法律声明和版权信息',
            'label': 'T3_Leaf_Direct_Select',
            'expected_decision': '',
            'env_overrides': {
                'RETRIEVAL_WALLET_TOTAL_BUDGET': '200000',
                'RETRIEVAL_WALLET_PER_RETRIEVE_STEP_BUDGET': '40000',
                'RETRIEVAL_AGENTIC_BOOTSTRAP_BUDGET': '2000',
                'RETRIEVAL_AGENTIC_PLANNING_RATIO': '0.5',
                'RETRIEVAL_AGENTIC_LATENCY_BUDGET_MS': '30000',
            },
        },
        # ── Case 4: No relevant document — query about a topic not in any document
        # Should result in no docs selected or empty evidence, testing the empty
        # evidence path and downstream notification.
        {
            'query': '量子计算对密码学的影响和后量子加密标准',
            'label': 'T4_No_Relevant_Doc',
            'expected_decision': '',
            'env_overrides': {
                'RETRIEVAL_WALLET_TOTAL_BUDGET': '200000',
                'RETRIEVAL_WALLET_PER_RETRIEVE_STEP_BUDGET': '40000',
                'RETRIEVAL_AGENTIC_BOOTSTRAP_BUDGET': '2000',
                'RETRIEVAL_AGENTIC_PLANNING_RATIO': '0.5',
                'RETRIEVAL_AGENTIC_LATENCY_BUDGET_MS': '30000',
            },
        },
        # ── Case 5: Broad overview — should STOP at root with sufficient_outline
        # Asks for an overview of the entire document, which should be answerable
        # from the root outline alone.
        {
            'query': '安全大模型技术与市场研究报告有哪些主要章节',
            'label': 'T5_Broad_Overview_STOP',
            'expected_decision': '',
            'env_overrides': {
                'RETRIEVAL_WALLET_TOTAL_BUDGET': '200000',
                'RETRIEVAL_WALLET_PER_RETRIEVE_STEP_BUDGET': '40000',
                'RETRIEVAL_AGENTIC_BOOTSTRAP_BUDGET': '2000',
                'RETRIEVAL_AGENTIC_PLANNING_RATIO': '0.5',
                'RETRIEVAL_AGENTIC_LATENCY_BUDGET_MS': '30000',
            },
        },
        # ── Case 6: Cross-doc — query touching construction doc (not security)
        # Tests that KG select picks the right document from a diverse corpus.
        {
            'query': '土方开挖施工安全保证措施有哪些',
            'label': 'T6_Cross_Doc_Construction',
            'expected_decision': '',
            'env_overrides': {
                'RETRIEVAL_WALLET_TOTAL_BUDGET': '200000',
                'RETRIEVAL_WALLET_PER_RETRIEVE_STEP_BUDGET': '40000',
                'RETRIEVAL_AGENTIC_BOOTSTRAP_BUDGET': '2000',
                'RETRIEVAL_AGENTIC_PLANNING_RATIO': '0.5',
                'RETRIEVAL_AGENTIC_LATENCY_BUDGET_MS': '30000',
            },
        },
        # ── Case 7: Asset search — SEARCH_IMAGES tool for chart/image queries
        # Tests the new SEARCH_IMAGES tool: Navigator should use SEARCH_IMAGES
        # with a semantic query, LLM filters from all images, only matching
        # charts are added to evidence via reconcile_deferred_assets.
        {
            'query': '帮我找出所有金融股票相关的图和折线图',
            'label': 'T7_Chart_Search_Images',
            'expected_decision': '',
            'env_overrides': {
                'RETRIEVAL_WALLET_TOTAL_BUDGET': '200000',
                'RETRIEVAL_WALLET_PER_RETRIEVE_STEP_BUDGET': '40000',
                'RETRIEVAL_AGENTIC_BOOTSTRAP_BUDGET': '2000',
                'RETRIEVAL_AGENTIC_PLANNING_RATIO': '0.5',
                'RETRIEVAL_AGENTIC_LATENCY_BUDGET_MS': '30000',
            },
        },
        # ── Case 8: 冯荣州身份证图片 — image asset search
        {
            'query': '冯荣州 的身份证图片发我',
            'label': 'T8_FRZ_ID_Image',
            'expected_decision': '',
            'env_overrides': {
                'RETRIEVAL_WALLET_TOTAL_BUDGET': '200000',
                'RETRIEVAL_WALLET_PER_RETRIEVE_STEP_BUDGET': '40000',
                'RETRIEVAL_AGENTIC_BOOTSTRAP_BUDGET': '2000',
                'RETRIEVAL_AGENTIC_PLANNING_RATIO': '0.5',
                'RETRIEVAL_AGENTIC_LATENCY_BUDGET_MS': '30000',
            },
        },
    ]

    # ── CLI parsing ──────────────────────────────────────────────────────
    import argparse
    parser = argparse.ArgumentParser(description='Agentic retrieval E2E debug runner')
    parser.add_argument(
        '--test', '-t', action='append', dest='test_filters', default=[],
        help='Filter tests by label substring (repeatable, e.g. --test T2 --test T6)',
    )
    parser.add_argument(
        '--output-dir', '-o', dest='output_dir', default=None,
        help='Base output directory for trace runs. Default: ~/Desktop/agentic_traces',
    )
    parser.add_argument(
        '--namespace', '-n', dest='namespace', default=None,
        help='Override the NAMESPACE used for DB queries.',
    )
    parser.add_argument(
        '--query', '-q', dest='query', default=None,
        help='Run one ad-hoc query through shared.services.retrieval.app_service.',
    )
    parser.add_argument(
        '--label', dest='label', default='adhoc',
        help='Trace label for --query output.',
    )
    parser.add_argument(
        '--top-k', dest='top_k', type=int, default=TOP_K,
        help=f'Top K for --query mode. Default: {TOP_K}.',
    )
    parser.add_argument(
        '--chunk-scope',
        choices=sorted(CHUNK_SCOPE_DATA_TYPE),
        default='all',
        help=(
            'Chunk type scope for --query mode: all=mixed, page=PAGE only, '
            'chunk=text/image/table only.'
        ),
    )
    parser.add_argument(
        '--data-type',
        dest='data_type',
        type=int,
        default=None,
        help='Override retrieval data_type for --query mode.',
    )
    parser.add_argument(
        '--print-evidence',
        action='store_true',
        help='Print full evidence_text to stdout in --query mode.',
    )
    # Also accept a positional arg for backward compat: `python debug_retrieval.py T6`
    parser.add_argument('positional_filter', nargs='?', default=None)
    args = parser.parse_args()

    # Merge positional filter into test_filters for backward compat
    if args.positional_filter and args.positional_filter not in args.test_filters:
        args.test_filters.append(args.positional_filter)

    if args.namespace:
        global NAMESPACE
        NAMESPACE = args.namespace

    if args.query:
        data_type = (
            args.data_type
            if args.data_type is not None
            else CHUNK_SCOPE_DATA_TYPE[args.chunk_scope]
        )
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_base_dir = args.output_dir or os.path.expanduser('~/Desktop/agentic_traces')
        safe_label = re.sub(r'[^\w\-]', '_', args.label)
        output_dir = os.path.join(
            os.path.expanduser(output_base_dir),
            f'{timestamp}_{safe_label}',
        )
        os.makedirs(output_dir, exist_ok=True)

        report = await run_single_query(
            query=args.query,
            label=args.label,
            top_k=args.top_k,
            data_type=data_type,
        )
        result_path = os.path.join(output_dir, 'result.json')
        trace_path = os.path.join(output_dir, 'trace.md')
        with open(result_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        with open(trace_path, 'w', encoding='utf-8') as f:
            f.write(_render_single_query_report(report))

        result = report.get('result') or {}
        evidence = result.get('evidence_text') or ''
        refs = result.get('referenced_chunks') or []
        rows = result.get('results') or []
        print('=' * 90)
        print(f"QUERY: {args.query}")
        print(
            f"router={result.get('router_used')} "
            f"stop={result.get('stop_reason', '')} "
            f"data_type={data_type} scope={args.chunk_scope}"
        )
        print(f"evidence_text chars: {len(evidence)}")
        print(f"referenced_chunks: {len(refs)}  results: {len(rows)}")
        print(f"TRACE: {output_dir}")
        if args.print_evidence:
            print("\n----- EVIDENCE TEXT -----\n")
            print(evidence)
        return

    docs = await phase1_contract()
    await phase2_scope_candidates(docs)

    # ── Filter tests ─────────────────────────────────────────────────────
    if args.test_filters:
        tests = [
            t for t in tests
            if any(f in t['label'] for f in args.test_filters)
        ]
        logger.info(f'  Filtered to {len(tests)} test(s) matching {args.test_filters}')
        if not tests:
            logger.error('No tests matched the filter(s). Available labels:')
            for t in tests:
                logger.error(f'  - {t["label"]}')
            return

    # ── Timestamped output folder for the entire run ─────────────────────
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_base_dir = args.output_dir or os.path.expanduser('~/Desktop/agentic_traces')
    output_dir = os.path.join(os.path.expanduser(output_base_dir), timestamp)
    os.makedirs(output_dir, exist_ok=True)

    all_reports = []

    for test in tests:
        report = await run_test(
            test['query'],
            test['label'],
            env_overrides=test.get('env_overrides'),
            expected_decision=test.get('expected_decision', ''),
        )
        all_reports.append(report)

        # Write per-query trace file into the SAME folder
        safe_name = re.sub(r'[^\w\-]', '_', report['label'])
        file_path = os.path.join(output_dir, f'{safe_name}.md')
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(_render_md_report([report]))
            logger.info(f'  Trace saved: {file_path}')
        except Exception as e:
            logger.error(f'  Failed to save trace {file_path}: {e}')

    # Write combined summary index
    index_path = os.path.join(output_dir, '_index.md')
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write('# Agentic E2E Trace Index\n\n')
        f.write(f'Generated: {datetime.now().isoformat()}\n\n')
        f.write('| # | Label | Query | Router | LLM Calls | Refs | Elapsed |\n')
        f.write('|:--|:------|:------|:-------|:----------|:-----|:--------|\n')
        for i, r in enumerate(all_reports, 1):
            safe_name = re.sub(r'[^\w\-]', '_', r['label'])
            f.write(
                f"| {i} | [{r['label']}]({safe_name}.md) "
                f"| {r['query'][:40]}… "
                f"| `{r['router_used']}` "
                f"| {r.get('llm_interactions', 0)} "
                f"| {r.get('referenced_chunks_count', 0)} "
                f"| {r['total_ms']}ms |\n"
            )

    logger.info('\n' + '=' * 80)
    logger.info(f'TRACE FOLDER saved to: {output_dir}')
    logger.info(f'  Index: {index_path}')
    logger.info(f'  Total tests: {len(all_reports)} traces')
    logger.info('=' * 80)

if __name__ == '__main__':
    asyncio.run(main())
