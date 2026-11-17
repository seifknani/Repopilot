"""The stop rule: hard limits that guarantee the agent loop terminates.

Written before the loop itself (PDF p.7: "Write the stopping condition first").

Termination argument (tested in tests/test_stop_rule.py):
  * Every loop iteration calls `begin_step()`, which increments `steps` by one, and the loop
    checks `before_step()` first, which stops once `steps >= max_steps`. So the loop body runs at
    most `max_steps` times, whatever the LLM answers.
  * Inside one iteration, LLM retries are bounded by `llm_max_retries`, and tool calls are bounded
    by `max_tool_calls` (checked before every call).
  * Every tool call has its own timeout (test runner and git).
  So the total work is bounded by max_steps × (llm_max_retries + 1) LLM calls and
  max_tool_calls tool calls. The token, cost, time, failure and loop-detection limits can only
  make the run stop *earlier*.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, fields
from enum import Enum
from typing import Callable, Hashable, Optional


class StopReason(str, Enum):
    DONE = "done"  # the agent gave a final answer (no tool call)
    MAX_STEPS = "max_steps"
    MAX_TOOL_CALLS = "max_tool_calls"
    MAX_TOKENS = "max_tokens"
    MAX_COST = "max_cost"
    TIMEOUT = "timeout"
    TOO_MANY_FAILURES = "too_many_failures"  # retry limit: consecutive failed tool calls
    LOOP_DETECTED = "loop_detected"  # the same call repeated without the repo changing
    LLM_ERROR = "llm_error"  # the model provider kept failing
    USER_STOPPED = "user_stopped"  # the human cancelled the run


@dataclass(frozen=True)
class Limits:
    max_steps: int = 20  # LLM turns
    max_tool_calls: int = 40
    max_tokens: int = 200_000  # input + output, whole run
    max_cost_usd: float = 1.00
    max_wall_time_s: float = 600.0  # agent time only; waiting for the human is not counted
    max_consecutive_failures: int = 3
    max_repeated_call: int = 2  # identical call allowed this many times per repository state
    llm_max_retries: int = 2  # extra attempts when the provider fails

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            minimum = 0 if f.name == "llm_max_retries" else 1e-9
            if value < minimum:
                raise ValueError(f"Limits.{f.name} must be positive (got {value})")


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens. The scripted LLM is free; real providers set their list price."""

    model: str = "scripted"
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1_000_000


class Budget:
    def __init__(self, limits: Limits, pricing: Pricing = Pricing(), clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self.pricing = pricing
        self.clock = clock
        self.steps = 0
        self.tool_calls = 0
        self.failed_tool_calls = 0
        self.consecutive_failures = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.llm_errors = 0
        self.active_seconds = 0.0
        self._segment_start: Optional[float] = None
        self._call_counts: Counter = Counter()

    # --- time (only while the agent is actually running) ------------------------------
    def start_segment(self) -> None:
        if self._segment_start is None:
            self._segment_start = self.clock()

    def end_segment(self) -> None:
        if self._segment_start is not None:
            self.active_seconds += self.clock() - self._segment_start
            self._segment_start = None

    @property
    def elapsed(self) -> float:
        running = self.clock() - self._segment_start if self._segment_start is not None else 0.0
        return self.active_seconds + running

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float:
        return self.pricing.cost(self.input_tokens, self.output_tokens)

    # --- checks ------------------------------------------------------------------------
    def _resource_check(self) -> Optional[StopReason]:
        if self.tokens >= self.limits.max_tokens:
            return StopReason.MAX_TOKENS
        if self.cost_usd >= self.limits.max_cost_usd:
            return StopReason.MAX_COST
        if self.elapsed >= self.limits.max_wall_time_s:
            return StopReason.TIMEOUT
        return None

    def before_step(self) -> Optional[StopReason]:
        if self.steps >= self.limits.max_steps:
            return StopReason.MAX_STEPS
        if self.tool_calls >= self.limits.max_tool_calls:
            return StopReason.MAX_TOOL_CALLS
        return self._resource_check()

    def begin_step(self) -> int:
        self.steps += 1
        return self.steps

    def record_llm(self, input_tokens: int, output_tokens: int) -> Optional[StopReason]:
        self.input_tokens += max(0, int(input_tokens))
        self.output_tokens += max(0, int(output_tokens))
        return self._resource_check()

    def record_llm_error(self) -> None:
        self.llm_errors += 1

    def before_tool(self, call_key: Hashable) -> Optional[StopReason]:
        if self.tool_calls >= self.limits.max_tool_calls:
            return StopReason.MAX_TOOL_CALLS
        if self._call_counts[call_key] >= self.limits.max_repeated_call:
            return StopReason.LOOP_DETECTED
        reason = self._resource_check()
        if reason:
            return reason
        self._call_counts[call_key] += 1
        self.tool_calls += 1
        return None

    def record_tool(self, ok: bool) -> Optional[StopReason]:
        if ok:
            self.consecutive_failures = 0
            return None
        self.failed_tool_calls += 1
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.limits.max_consecutive_failures:
            return StopReason.TOO_MANY_FAILURES
        return None

    def snapshot(self) -> dict:
        return {
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "failed_tool_calls": self.failed_tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed_s": round(self.elapsed, 3),
            "llm_errors": self.llm_errors,
            "model": self.pricing.model,
        }
