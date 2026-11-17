"""Proof that the agent loop always stops (PDF p.4 "prove it stops", p.7 "test that it holds").

Every test drives the REAL loop and REAL tools with a scripted LLM that never gives up.
"""
import random

import pytest

from agent.llm import LLMError, LLMResponse, ScriptedLLM, call, say
from functools import partial

from agent.loop import Agent as _Agent, RunStatus
from agent.stop_rule import Limits, Pricing, StopReason

# These tests measure the loop's own limits; the baseline test run (a system call) is not needed.
Agent = partial(_Agent, baseline_tests=False)


def endless_reader():
    """An LLM that never finishes: each turn it reads the next line of a file (always new args)."""
    counter = {"n": 0}

    def step(_messages):
        counter["n"] += 1
        return say("Reading one more line.", call("read_file", path="app/database.py",
                                                    start_line=counter["n"] % 100 + 1, end_line=counter["n"] % 100 + 1))
    return ScriptedLLM([step], repeat_last=True)


@pytest.mark.parametrize("max_steps", [1, 2, 5, 12])
def test_endless_agent_stops_exactly_at_max_steps(executor, max_steps):
    llm = endless_reader()
    agent = Agent(executor, llm, Limits(max_steps=max_steps, max_tool_calls=1000, max_repeated_call=1000))
    result = agent.start("Endless investigation")
    assert result.status == RunStatus.STOPPED and result.stop_reason == StopReason.MAX_STEPS
    assert llm.calls == max_steps
    assert result.metrics["steps"] == max_steps
    assert result.metrics["tool_calls"] == max_steps


def test_tool_call_limit_with_many_calls_in_one_turn(executor):
    calls = [call("read_file", path="app/database.py", start_line=i, end_line=i) for i in range(1, 51)]
    agent = Agent(executor, ScriptedLLM([say("Read everything.", *calls)], repeat_last=True), Limits(max_tool_calls=7))
    result = agent.start("x")
    assert result.stop_reason == StopReason.MAX_TOOL_CALLS
    executed = [r for r in executor.logger.records if r["type"] == "tool_call" and r["data"].get("error_type") != "refused_by_loop"]
    assert len(executed) == 7


def test_token_limit(executor):
    llm = ScriptedLLM([say("think", call("git_status"), input_tokens=30_000, output_tokens=1_000),
                       say("think", call("git_log"), input_tokens=30_000, output_tokens=1_000),
                       say("think", call("list_files"), input_tokens=30_000, output_tokens=1_000)])
    result = Agent(executor, llm, Limits(max_tokens=50_000)).start("x")
    assert result.stop_reason == StopReason.MAX_TOKENS and llm.calls == 2


def test_cost_limit(executor):
    llm = ScriptedLLM([say("think", call("git_status"), input_tokens=100_000, output_tokens=10_000)], repeat_last=True)
    pricing = Pricing("priced-model", input_per_mtok=3.0, output_per_mtok=15.0)  # $0.45 per turn
    result = Agent(executor, llm, Limits(max_cost_usd=1.0, max_tokens=10_000_000, max_repeated_call=100), pricing).start("x")
    assert result.stop_reason == StopReason.MAX_COST
    assert llm.calls == 3 and result.metrics["cost_usd"] == pytest.approx(1.35)


def test_timeout_with_fake_clock(executor):
    clock = {"t": 0.0}

    def slow_step(_messages):
        clock["t"] += 40  # every model turn "takes" 40 seconds
        return say("slow", call("git_status"))
    llm = ScriptedLLM([slow_step], repeat_last=True)
    result = Agent(executor, llm, Limits(max_wall_time_s=100, max_repeated_call=100), clock=lambda: clock["t"]).start("x")
    assert result.stop_reason == StopReason.TIMEOUT and llm.calls == 3


def test_consecutive_tool_failures_stop_the_run(executor):
    counter = {"n": 0}

    def bad(_messages):
        counter["n"] += 1
        return say("Try another file.", call("read_file", path=f"app/missing_{counter['n']}.py"))
    result = Agent(executor, ScriptedLLM([bad], repeat_last=True), Limits(max_consecutive_failures=3)).start("x")
    assert result.stop_reason == StopReason.TOO_MANY_FAILURES
    assert result.metrics["failed_tool_calls"] == 3 and result.metrics["steps"] == 3


def test_identical_call_loop_is_detected(executor):
    llm = ScriptedLLM([say("Check status again.", call("git_status"))], repeat_last=True)
    result = Agent(executor, llm, Limits(max_repeated_call=2)).start("x")
    assert result.stop_reason == StopReason.LOOP_DETECTED
    assert result.metrics["tool_calls"] == 2 and llm.calls == 3


def test_provider_down_stops_after_retries(executor):
    llm = ScriptedLLM([LLMError("503 Service Unavailable")], repeat_last=True)
    result = Agent(executor, llm, Limits(llm_max_retries=2)).start("x")
    assert result.stop_reason == StopReason.LLM_ERROR
    assert llm.calls == 3 and result.metrics["llm_errors"] == 3


def test_transient_provider_error_is_retried(executor):
    llm = ScriptedLLM([LLMError("timeout"), say("All good, nothing to do.")])
    result = Agent(executor, llm, Limits(llm_max_retries=1)).start("x")
    assert result.status == RunStatus.DONE and result.metrics["llm_errors"] == 1


@pytest.mark.parametrize("seed", range(25))
def test_random_agents_always_respect_every_limit(executor, seed):
    """Fuzz: random mixes of good calls, bad calls, repeats, errors and no-ops. The run must end
    and no counter may ever exceed its limit."""
    rng = random.Random(seed)
    choices = [
        lambda: call("list_files", directory=rng.choice([".", "app", "tests", "nope"])),
        lambda: call("read_file", path=rng.choice(["app/main.py", "app/x.py", "../etc"]), start_line=rng.randint(1, 80)),
        lambda: call("search_code", query=rng.choice(["status", "priority", "zz"])),
        lambda: call("git_status"),
        lambda: call("unknown_tool", x=1),
        lambda: call("apply_edit", proposal_id="edit-000000000000"),
        lambda: call("read_file", bogus=True),
    ]

    def turn(_messages):
        if rng.random() < 0.1:
            return LLMError("random outage")
        return say("random step", *[rng.choice(choices)() for _ in range(rng.randint(1, 4))])

    limits = Limits(max_steps=rng.randint(1, 15), max_tool_calls=rng.randint(1, 30),
                    max_consecutive_failures=rng.randint(1, 5), max_repeated_call=rng.randint(1, 3))
    llm = ScriptedLLM([turn], repeat_last=True)
    result = Agent(executor, llm, limits).start("fuzz")
    assert result.status == RunStatus.STOPPED
    assert result.metrics["steps"] <= limits.max_steps
    assert result.metrics["tool_calls"] <= limits.max_tool_calls
    assert llm.calls <= limits.max_steps * (limits.llm_max_retries + 1)
