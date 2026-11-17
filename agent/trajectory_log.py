"""JSONL trajectory log: one line per tool call or agent event.

Named trajectory_log (not logging) to avoid shadowing Python's standard `logging` module.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

MAX_LOGGED_OUTPUT = 4_000


class TrajectoryLogger:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else None
        self.records: list[dict[str, Any]] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        self.records.append(record)
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        return record

    def log_tool_call(
        self, *, tool: str, args: Any, ok: bool, output: str, error: Optional[str],
        data: dict[str, Any], duration_ms: float, actor: str, step: Optional[int] = None,
    ) -> dict[str, Any]:
        truncated = len(output) > MAX_LOGGED_OUTPUT
        return self._write({
            "type": "tool_call",
            "step": step,
            "actor": actor,
            "tool": tool,
            "input": args,
            "ok": ok,
            "error": error,
            "output": output[:MAX_LOGGED_OUTPUT] + ("\n...[truncated in log]" if truncated else ""),
            "data": data,
            "duration_ms": round(duration_ms, 2),
        })

    def log_event(self, event: str, **fields: Any) -> dict[str, Any]:
        return self._write({"type": event, **fields})
