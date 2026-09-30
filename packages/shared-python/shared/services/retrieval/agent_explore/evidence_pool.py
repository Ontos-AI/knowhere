"""Per-round addresses, pick ids, and the episode evidence pool.

Results of one round become usable only when the next round begins: their
addresses become readable, and their pick ids open a pick phase.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_tools import Decision, ReadableAddresses, ToolResult
from shared.services.retrieval.hydration.evidence_compose import group_evidence_units
from shared.services.retrieval.hydration.row_utils import (
    iter_connected_target_ids,
    normalize_chunk_type,
)
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
        self.round_index = 0
        self.pick_phase = False
        self._read_calls = 0
        self._outline_calls = 0
        self._chunk_keys: set[tuple[str, str]] = set()
        self._seen_active: set[tuple[str, str]] = set()
        self._seen_staged: set[tuple[str, str]] = set()
        self._batch: dict[str, Candidate] = {}
        self._batch_staged: dict[str, Candidate] = {}
        self._batch_round = 0
        self._decided: dict[tuple[str, str], Decision] = {}

    def begin_round(self) -> None:
        self.round_index += 1
        self._seen_active |= self._seen_staged
        self._seen_staged = set()
        if self._batch_staged:
            self._batch.update(self._batch_staged)
            self._batch_staged = {}
            self._batch_round = self.round_index - 1
        self.pick_phase = bool(self._batch)

    def issue(self, tool_name: str, result: ToolResult) -> list[Candidate]:
        if result.error:
            return []
        self._collect_seen(result)
        if tool_name == "corpus.read":
            return self._issue_read(result)
        if tool_name == "corpus.outline":
            return self._issue_outline(result)
        return []

    def apply_pick(self, handles: list[str]) -> PickOutcome:
        outcome = PickOutcome()
        seen: set[str] = set()
        for handle in handles:
            if handle in seen:
                continue
            seen.add(handle)
            candidate = self._batch.get(handle)
            if candidate is None:
                outcome.rejected.append({"handle": handle, "reason": "unknown id"})
                continue
            if candidate.kind == "read" and self._all_chunks_in_pool(candidate):
                outcome.rejected.append({"handle": handle, "reason": "already in pool"})
                continue
            self.entries.append(candidate)
            for chunk_id in candidate.chunk_ids:
                self._chunk_keys.add((candidate.document_id, chunk_id))
            outcome.added.append(handle)
        picked = set(outcome.added)
        for candidate in self._batch.values():
            if candidate.kind != "read":
                continue
            picked_handle = candidate.handle if candidate.handle in picked else None
            for chunk_id in candidate.chunk_ids:
                key = (candidate.document_id, chunk_id)
                existing = self._decided.get(key)
                if existing is not None and existing.picked_handle is not None:
                    continue
                self._decided[key] = Decision(
                    read_round=self._batch_round,
                    picked_handle=picked_handle,
                    handle=candidate.handle,
                )
        self._batch = {}
        self.pick_phase = False
        return outcome

    def readable(self) -> ReadableAddresses:
        return frozenset(self._seen_active)

    def decided(self) -> Mapping[tuple[str, str], Decision]:
        return MappingProxyType(dict(self._decided))

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

    def _collect_seen(self, result: ToolResult) -> None:
        for key in ("rows", "chunks"):
            rows = result.payload.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                document_id = str(row.get("document_id") or "")
                if not document_id:
                    continue
                section_path = str(row.get("section_path") or "").strip()
                if section_path:
                    self._seen_staged.add((document_id, section_path))
                chunk_ids = [
                    row.get("chunk_id"),
                    *(row.get("mounted_chunk_ids") or []),
                    *iter_connected_target_ids(row),
                ]
                for chunk_id in chunk_ids:
                    if chunk_id:
                        self._seen_staged.add((document_id, str(chunk_id)))

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
            self._batch_staged[candidate.handle] = candidate
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
        self._batch_staged[candidate.handle] = candidate
        return [candidate]


def render_budget(budget: EpisodeBudget) -> str:
    return f"budget: steps {budget.steps_used}/{budget.max_steps}"


def render_pick_outcome(outcome: PickOutcome) -> str:
    text = f"picked: {', '.join(outcome.added) or 'none'}"
    if outcome.rejected:
        rejected = ", ".join(
            f"{item['handle']} ({item['reason']})" for item in outcome.rejected
        )
        text += f"; rejected: {rejected}"
    return text


def render_trace_line(
    index: int,
    wire_name: str,
    args: dict[str, Any],
    status: str,
) -> str:
    encoded = json.dumps(args, ensure_ascii=False, default=str)
    return f"{index}. {wire_name} {encoded} -> {status}"


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
    units: list[dict[str, Any]] = []
    for candidate in entries:
        if candidate.kind == "outline":
            text = "\n".join(candidate.outline_lines)
            if not text:
                continue
            units.append(
                {
                    "document_id": candidate.document_id,
                    "source_file_name": candidate.source_file_name,
                    "section_path": "",
                    "sort_order": None,
                    "parts": [{"type": "text", "text": text}],
                    "kind": "outline",
                }
            )
            continue
        chunk_rows = sorted(
            (rows_by_id[chunk_id] for chunk_id in candidate.chunk_ids if chunk_id in rows_by_id),
            key=lambda row: row["sort_order"],
        )
        parts = [part for row in chunk_rows for part in row["composed"]]
        if not parts:
            continue
        units.append(
            {
                "document_id": candidate.document_id,
                "source_file_name": candidate.source_file_name,
                "section_path": candidate.section_path or "",
                "sort_order": chunk_rows[0]["sort_order"],
                "parts": parts,
                "kind": "read",
            }
        )
    return group_evidence_units(units)


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


def trace_status(result: ToolResult) -> str:
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
