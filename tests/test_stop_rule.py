"""Unit tests for the stop rule (agent/stop_rule.py), independent of the loop."""
import pytest

from agent.stop_rule import Budget, Limits, Pricing, StopReason


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize("field", ["max_steps", "max_tool_calls", "max_tokens", "max_cost_usd",
                                   "max_wall_time_s", "max_consecutive_failures", "max_repeated_call"])
@pytest.mark.parametrize("value", [0, -1])
def test_limits_must_be_positive(field, value):
    with pytest.raises(ValueError):
        Limits(**{field: value})


def test_step_limit_is_exact():
    budget = Budget(Limits(max_steps=3))
    iterations = 0
    while budget.before_step() is None:
        budget.begin_step()
        iterations += 1
        assert iterations <= 3, "budget allowed more steps than the limit"
    assert iterations == 3 and budget.before_step() == StopReason.MAX_STEPS


def test_tool_call_limit():
    budget = Budget(Limits(max_tool_calls=2, max_repeated_call=10))
    assert budget.before_tool("a") is None
    assert budget.before_tool("b") is None
    assert budget.before_tool("c") == StopReason.MAX_TOOL_CALLS
    assert budget.tool_calls == 2


def test_token_and_cost_limits():
    budget = Budget(Limits(max_tokens=1_000))
    assert budget.record_llm(400, 100) is None
    assert budget.record_llm(400, 100) == StopReason.MAX_TOKENS

    priced = Budget(Limits(max_cost_usd=0.01), Pricing("m", input_per_mtok=10.0, output_per_mtok=10.0))
    assert priced.record_llm(500, 0) is None  # $0.005
    assert priced.record_llm(500, 0) == StopReason.MAX_COST  # $0.010
    assert priced.snapshot()["cost_usd"] == pytest.approx(0.01)


def test_wall_time_excludes_human_wait():
    clock = FakeClock()
    budget = Budget(Limits(max_wall_time_s=10), clock=clock)
    budget.start_segment()
    clock.now = 6
    budget.end_segment()  # agent paused for approval
    clock.now = 1_000  # the human took a long time
    budget.start_segment()
    assert budget.before_step() is None and budget.elapsed == pytest.approx(6)
    clock.now = 1_004
    assert budget.before_step() == StopReason.TIMEOUT


def test_consecutive_failures_reset_on_success():
    budget = Budget(Limits(max_consecutive_failures=3))
    assert budget.record_tool(False) is None
    assert budget.record_tool(False) is None
    assert budget.record_tool(True) is None  # success resets the streak
    assert budget.record_tool(False) is None
    assert budget.record_tool(False) is None
    assert budget.record_tool(False) == StopReason.TOO_MANY_FAILURES
    assert budget.failed_tool_calls == 5


def test_repeated_identical_call_is_a_loop():
    budget = Budget(Limits(max_repeated_call=2))
    key = ("read_file", '{"path": "app/main.py"}', 0)
    assert budget.before_tool(key) is None
    assert budget.before_tool(key) is None
    assert budget.before_tool(key) == StopReason.LOOP_DETECTED
    assert budget.tool_calls == 2  # the looping call was not counted as executed
    assert budget.before_tool(("read_file", '{"path": "app/main.py"}', 1)) is None  # new repo state
