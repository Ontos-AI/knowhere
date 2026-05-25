"""Best-effort parse-agent trace buffering and database persistence."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from app.services.document_agent.manifest import PageAnatomyMap, ToolResult


class ParseRunRecorder:
    def __init__(self, *, job_id: str, db: Any | None = None) -> None:
        self.run_id = f"prof_{uuid4().hex[:12]}"
        self.job_id = job_id
        self._db = db
        self._started = time.monotonic()
        self._steps: list[dict[str, Any]] = []
        self._anatomy: PageAnatomyMap | None = None
        self._artifact_path: str | None = None

    def record_step(
        self,
        *,
        round_index: int,
        actor: str,
        action_type: str,
        result: ToolResult,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
    ) -> None:
        self._steps.append(
            {
                "round_index": round_index,
                "actor": actor,
                "action_type": action_type,
                "tool_name": tool_name,
                "tool_args": tool_args or {},
                "observation": {
                    "status": result.status,
                    "payload_keys": sorted(result.payload.keys()),
                    "payload": result.payload,
                    "input_summary": result.input_summary,
                    "output_summary": result.output_summary,
                    "warnings": list(result.warnings),
                    "debug": result.debug,
                    "error": result.error,
                },
                "tokens_used": result.tokens_used,
                "latency_ms": result.latency_ms,
                "created_at": datetime.utcnow(),
            }
        )

    def set_anatomy_map(self, anatomy: PageAnatomyMap, artifact_path: str) -> None:
        self._anatomy = anatomy
        self._artifact_path = artifact_path
        self.write_trace_json(str(Path(artifact_path).with_name("trace.json")))

    def write_trace_artifact(
        self,
        output_dir: str | None,
        *,
        final_status: str,
        summary: dict[str, Any] | None = None,
    ) -> None:
        if output_dir is None:
            return
        self.write_trace_json(
            str(Path(output_dir) / "trace.json"),
            final_status=final_status,
            summary=summary,
        )

    def write_trace_json(
        self,
        trace_path: str,
        *,
        final_status: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        try:
            import json

            serializable_steps = []
            for step in self._steps:
                item = dict(step)
                created_at = item.get("created_at")
                if created_at is not None and hasattr(created_at, "isoformat"):
                    item["created_at"] = created_at.isoformat()
                serializable_steps.append(item)
            Path(trace_path).write_text(
                json.dumps(
                    {
                        "run_id": self.run_id,
                        "job_id": self.job_id,
                        "final_status": final_status,
                        "summary": summary,
                        "artifact_path": self._artifact_path,
                        "steps": serializable_steps,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug(f"parse agent trace json write failed: {exc}")

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "step_count": len(self._steps),
            "artifact_path": self._artifact_path,
            "latency_ms": int((time.monotonic() - self._started) * 1000),
        }

    def flush(self, *, final_status: str, summary: dict[str, Any] | None = None) -> None:
        if self._db is None:
            return
        try:
            from shared.models.database.parse_agent import ParseRun, ParseStep
            from shared.models.database.document_page_plan import DocumentPagePlan

            run = ParseRun(
                run_id=self.run_id,
                job_id=self.job_id,
                kind="profile",
                final_status=final_status,
                rounds_count=max((step["round_index"] for step in self._steps), default=0) + 1,
                total_tokens=sum(int(step.get("tokens_used") or 0) for step in self._steps),
                total_latency_ms=int((time.monotonic() - self._started) * 1000),
                summary=summary or self.summary(),
            )
            self._db.add(run)
            for index, step in enumerate(self._steps):
                self._db.add(
                    ParseStep(
                        step_id=f"prst_{uuid4().hex[:12]}",
                        run_id=self.run_id,
                        round_index=int(step["round_index"]),
                        actor=str(step["actor"]),
                        action_type=str(step["action_type"]),
                        tool_name=step.get("tool_name"),
                        tool_args=step.get("tool_args"),
                        observation=step.get("observation"),
                        tokens_used=int(step.get("tokens_used") or 0),
                        latency_ms=int(step.get("latency_ms") or 0),
                        created_at=step.get("created_at"),
                    )
                )
            if self._anatomy is not None:
                self._db.add(
                    DocumentPagePlan(
                        page_plan_id=f"dpp_{uuid4().hex[:12]}",
                        job_id=self.job_id,
                        page_count=self._anatomy.page_count,
                        shard_plan=self._anatomy.shard_plan.to_dict(),
                        global_signals=self._anatomy.global_signals,
                    )
                )
            self._db.flush()
        except Exception as exc:
            logger.debug(f"parse agent trace flush failed: {exc}")
            try:
                self._db.rollback()
            except Exception:
                pass
