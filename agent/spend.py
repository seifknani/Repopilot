"""Semester spending ledger: every real LLM call is recorded, and the agent refuses to call the
model once the total reaches the cap.

    REPOPILOT_BUDGET_USD    total cap for all runs (default 18.00: the team's $20 minus a margin)
    REPOPILOT_SPEND_LEDGER  ledger file (default logs/spend.jsonl, git-ignored)

The per-run limit (Limits.max_cost_usd) stops one runaway task; this ledger stops the sum of
all tasks (UI, issue runner, eval) from exceeding the team budget. Costs are computed from our
price table, so they are an estimate: the provider's billing page is the authoritative number,
and a hard monthly limit on the provider's project is the last line of defence.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_BUDGET_USD = 18.00


class SpendLedger:
    def __init__(self, path: Path, cap_usd: float = DEFAULT_BUDGET_USD) -> None:
        self.path = Path(path)
        self.cap_usd = float(cap_usd)
        self._total: Optional[float] = None

    def total(self) -> float:
        if self._total is None:
            total = 0.0
            if self.path.is_file():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        total += float(json.loads(line).get("cost_usd", 0.0))
                    except (ValueError, AttributeError):
                        continue  # a damaged line never blocks the ledger
            self._total = total
        return self._total

    def remaining(self) -> float:
        return self.cap_usd - self.total()

    def exhausted(self) -> bool:
        return self.remaining() <= 0

    def record(self, cost_usd: float, **fields: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "cost_usd": round(float(cost_usd), 8), **fields}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        self._total = self.total() + float(cost_usd)

    def summary(self) -> str:
        return f"${self.total():.4f} spent of ${self.cap_usd:.2f} (${max(self.remaining(), 0):.4f} left)"


def ledger_from_env(project_root: Path) -> SpendLedger:
    path = os.environ.get("REPOPILOT_SPEND_LEDGER") or str(Path(project_root) / "logs" / "spend.jsonl")
    cap = float(os.environ.get("REPOPILOT_BUDGET_USD") or DEFAULT_BUDGET_USD)
    return SpendLedger(Path(path), cap)
