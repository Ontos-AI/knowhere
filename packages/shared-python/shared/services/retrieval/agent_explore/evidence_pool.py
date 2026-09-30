"""Per-step pick ids and the episode evidence pool."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_tools import ToolResult
from shared.services.retrieval.hydration.row_utils import normalize_chunk_type
from shared.services.retrieval.settings import ASSET_CHUNK_TYPES


@dataclass(frozen=True)
class Candidate:
    handle: str
    kind: Literal["read", "outline"]
    document_id: str
    source_file_name: str
    section_path: str | None = None
    chunk_type: str | None = None
    chunk_ids: tuple[str, ...] = ()
    summary: str = ""
    extra_sections: int = 0
    outline_lines: tuple[str, ...] = ()


@dataclass
class PickOutcome:
    added: list[str] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)


class EvidencePool:
    def __init__(self) -> None:
        self.entries: list[Candidate] = []
        self._pending: dict[str, Candidate] = {}
        self._read_calls = 0
        self._outline_calls = 0
        self._chunk_keys: set[tuple[str, str]] = set()

    def issue(self, tool_name: str, result: ToolResult) -> list[Candidate]:
        if result.error:
            return []
        if tool_name == "corpus.read":
            return self._issue_read(result)
        if tool_name == "corpus.outline":
            return self._issue_outline(result)
        return []

    def take_pending(self) -> dict[str, Candidate]:
        pending = self._pending
        self._pending = {}
        return pending

    def apply_pick(
        self, handles: list[str], pending: Mapping[str, Candidate]
    ) -> PickOutcome:
        outcome = PickOutcome()
        seen: set[str] = set()
        for handle in handles:
            if handle in seen:
                continue
            seen.add(handle)
            candidate = pending.get(handle)
            if candidate is None:
                outcome.rejected.append(
                    {"handle": handle, "reason": "expired or unknown"}
                )
                continue
            if candidate.kind == "read" and self._all_chunks_in_pool(candidate):
                outcome.rejected.append({"handle": handle, "reason": "already in pool"})
                continue
            self.entries.append(candidate)
            for chunk_id in candidate.chunk_ids:
                self._chunk_keys.add((candidate.document_id, chunk_id))
            outcome.added.append(handle)
        return outcome

    def render_candidates(self, candidates: list[Candidate]) -> str:
        if not candidates:
            return ""
        if len(candidates) == 1 and candidates[0].kind == "outline":
            return f"[pick id for this result: {candidates[0].handle} = this outline]"
        parts: list[str] = []
        for candidate in candidates:
            label = candidate.section_path or candidate.handle
            parts.append(f"{candidate.handle} = {label}")
        return f"[pick ids for this result: {', '.join(parts)}]"

    def render_pool(self) -> str:
        if not self.entries:
            return "evidence pool: empty"
        lines = [f"evidence pool ({len(self.entries)}):"]
        for candidate in self.entries:
            if candidate.kind == "outline":
                lines.extend(candidate.outline_lines)
                continue
            path = candidate.section_path or ""
            kind = candidate.chunk_type or "text"
            lines.append(
                f"  [{candidate.handle}] {candidate.source_file_name} / {path} [{kind}]"
            )
            if candidate.summary:
                lines.append(f"    summary: {candidate.summary}")
        return "\n".join(lines)

    def chunk_refs(self) -> list[dict[str, str]]:
        return pool_chunk_refs(self.entries)

    def _all_chunks_in_pool(self, candidate: Candidate) -> bool:
        if not candidate.chunk_ids:
            return False
        return all(
            (candidate.document_id, chunk_id) in self._chunk_keys
            for chunk_id in candidate.chunk_ids
        )

    def _issue_read(self, result: ToolResult) -> list[Candidate]:
        self._read_calls += 1
        statuses = result.payload.get("refs")
        chunks = result.payload.get("chunks")
        if not isinstance(statuses, list):
            return []
        chunk_rows = chunks if isinstance(chunks, list) else []
        rows_by_id = {
            str(row.get("chunk_id") or ""): row
            for row in chunk_rows
            if isinstance(row, dict) and row.get("chunk_id")
        }
        issued: list[Candidate] = []
        for index, entry in enumerate(statuses, start=1):
            if not isinstance(entry, dict) or entry.get("status") != "ok":
                continue
            chunk_ids = tuple(
                str(item) for item in (entry.get("chunk_ids") or []) if str(item)
            )
            first = rows_by_id.get(chunk_ids[0]) if chunk_ids else None
            document_id = str(entry.get("document_id") or "")
            section_path = str(entry.get("section_path") or "").strip() or None
            if first and not section_path:
                section_path = str(first.get("section_path") or "").strip() or None
            chunk_type = normalize_chunk_type((first or {}).get("chunk_type"))
            summary = _read_summary(first, chunk_type)
            candidate = Candidate(
                handle=f"R{self._read_calls}.{index}",
                kind="read",
                document_id=document_id,
                source_file_name=str((first or {}).get("source_file_name") or ""),
                section_path=section_path,
                chunk_type=chunk_type or None,
                chunk_ids=chunk_ids,
                summary=summary,
                extra_sections=max(len(chunk_ids) - 1, 0),
            )
            issued.append(candidate)
            self._pending[candidate.handle] = candidate
        return issued

    def _issue_outline(self, result: ToolResult) -> list[Candidate]:
        self._outline_calls += 1
        rows = result.payload.get("rows")
        details = result.payload.get("details")
        if not isinstance(rows, list) or not rows:
            return []
        documents = details.get("documents") if isinstance(details, dict) else None
        names: Mapping[str, Any] = documents if isinstance(documents, dict) else {}
        handle = f"O{self._outline_calls}"
        first = rows[0] if isinstance(rows[0], dict) else {}
        document_id = str(first.get("document_id") or "")
        source_file_name = str(names.get(document_id) or first.get("title") or "")
        lines = _outline_title_tree(rows, names, handle=handle)
        candidate = Candidate(
            handle=handle,
            kind="outline",
            document_id=document_id,
            source_file_name=source_file_name,
            outline_lines=tuple(lines),
        )
        self._pending[candidate.handle] = candidate
        return [candidate]


def render_budget(budget: EpisodeBudget) -> str:
    snap = budget.snapshot()
    return (
        f"budget: steps {snap['steps_used']}/{snap['max_steps']}, "
        f"tokens {snap['tokens_used']}/{snap['token_limit']}, "
        f"elapsed {snap['elapsed_seconds']:.0f}s/{snap['wall_clock_seconds']:.0f}s"
    )


def render_trace_line(
    index: int,
    wire_name: str,
    args: dict[str, Any],
    result: ToolResult,
) -> str:
    encoded = json.dumps(args, ensure_ascii=False, default=str)
    return f"{index}. {wire_name} {encoded} -> {_trace_status(result)}"


def pool_chunk_refs(entries: list[Candidate]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for candidate in entries:
        if candidate.kind != "read":
            continue
        for chunk_id in candidate.chunk_ids:
            refs.append({"document_id": candidate.document_id, "chunk_id": chunk_id})
    return refs


def compose_pool_evidence(
    entries: list[Candidate],
    assembled_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows_by_id = {
        str(row.get("chunk_id") or ""): row
        for row in assembled_rows
        if row.get("chunk_id")
    }
    parts: list[dict[str, Any]] = []
    for candidate in entries:
        if candidate.kind == "outline":
            text = "\n".join(candidate.outline_lines)
            if text:
                parts.append({"type": "text", "text": text})
            continue
        for chunk_id in candidate.chunk_ids:
            row = rows_by_id.get(chunk_id)
            composed = row.get("composed") if row else None
            if isinstance(composed, list):
                parts.extend(item for item in composed if isinstance(item, dict))
    return parts


def pool_trace_records(entries: list[Candidate]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for candidate in entries:
        records.append(
            {
                "handle": candidate.handle,
                "kind": candidate.kind,
                "document_id": candidate.document_id,
                "section_path": candidate.section_path,
                "chunk_ids": list(candidate.chunk_ids),
            }
        )
    return records


def _trace_status(result: ToolResult) -> str:
    if result.error:
        return f"failed: {result.error}"
    statuses = result.payload.get("refs")
    if isinstance(statuses, list):
        failures: list[str] = []
        for index, entry in enumerate(statuses, start=1):
            if isinstance(entry, dict) and entry.get("status") == "failed":
                reason = str(entry.get("reason") or "failed")
                failures.append(f"ref {index} failed: {reason}")
        if failures:
            return "partial: " + "; ".join(failures)
    return "ok"


def _read_summary(row: dict[str, Any] | None, chunk_type: str) -> str:
    if not row:
        return ""
    if chunk_type in ASSET_CHUNK_TYPES:
        metadata = row.get("chunk_metadata") or {}
        if isinstance(metadata, dict):
            return str(metadata.get("summary") or "").strip()
        return ""
    return str(row.get("section_summary") or "").strip()


def _outline_title_tree(
    rows: list[Any], names: Mapping[str, Any], *, handle: str
) -> list[str]:
    lines: list[str] = []
    current_doc = ""
    first_doc = True
    for row in rows:
        if not isinstance(row, dict):
            continue
        document_id = str(row.get("document_id") or "")
        if document_id != current_doc:
            current_doc = document_id
            file_name = str(names.get(document_id) or row.get("title") or document_id)
            if first_doc:
                lines.append(f"  [{handle}] {file_name}")
                first_doc = False
            else:
                lines.append(f"  {file_name}")
        title = str(row.get("title") or "").strip()
        if title:
            depth = int(row.get("depth") or 0)
            lines.append(f"{'  ' * (depth + 1)}{title}")
    return lines
