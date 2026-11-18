"""Team spending ledger and the compact baseline message (cost optimisation)."""
import json

from agent.demo_scripts import build_script
from agent.llm import ScriptedLLM, call, say
from agent.loop import Agent
from agent.spend import SpendLedger, ledger_from_env
from agent.stop_rule import Limits, Pricing, StopReason

NO_RUN_CAP = Limits(max_cost_usd=1e9, max_repeated_call=100)  # isolate the team ledger from the per-run limit

PRICE = Pricing("m", input_per_mtok=1_000_000.0, output_per_mtok=0.0)  # $1 per input token: easy numbers


def test_ledger_sums_and_survives_a_damaged_line(tmp_path):
    path = tmp_path / "spend.jsonl"
    ledger = SpendLedger(path, cap_usd=1.0)
    ledger.record(0.25, model="m")
    path.open("a").write("not json\n")
    ledger.record(0.5, model="m")
    fresh = SpendLedger(path, cap_usd=1.0)
    assert fresh.total() == 0.75 and not fresh.exhausted() and "0.2500 left" in fresh.summary()
    fresh.record(0.25)
    assert fresh.exhausted()


def test_every_llm_call_is_recorded_with_its_cost(executor, tmp_path):
    ledger = SpendLedger(tmp_path / "spend.jsonl", cap_usd=1_000.0)
    agent = Agent(executor, ScriptedLLM([say("a", call("git_status"), input_tokens=3), say("done", input_tokens=4)]),
                  limits=NO_RUN_CAP, pricing=PRICE, ledger=ledger, baseline_tests=False, run_metadata={"issue": 9})
    agent.start("x")
    lines = [json.loads(line) for line in (tmp_path / "spend.jsonl").read_text().splitlines()]
    assert [line["cost_usd"] for line in lines] == [3.0, 4.0]
    assert lines[0]["issue"] == 9 and lines[0]["thread_id"] == agent.thread_id


def test_agent_stops_when_the_team_budget_is_used_up(executor, tmp_path):
    ledger = SpendLedger(tmp_path / "spend.jsonl", cap_usd=5.0)
    llm = ScriptedLLM([say("think", call("git_status"), input_tokens=3)], repeat_last=True)
    result = Agent(executor, llm, limits=NO_RUN_CAP, pricing=PRICE, ledger=ledger, baseline_tests=False).start("x")
    assert result.stop_reason == StopReason.MAX_COST and "team LLM budget" in result.final_message
    assert llm.calls == 2 and ledger.total() == 6.0  # stops before the 3rd call; overshoot <= one call


def test_ledger_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOPILOT_BUDGET_USD", "7.5")
    monkeypatch.delenv("REPOPILOT_SPEND_LEDGER", raising=False)
    ledger = ledger_from_env(tmp_path)
    assert ledger.cap_usd == 7.5 and ledger.path == tmp_path / "logs" / "spend.jsonl"


def test_baseline_message_is_compact(executor):
    llm = ScriptedLLM(build_script("BUG-1"))
    Agent(executor, llm).start("status filter broken")
    first = llm.seen_messages[0][0]["content"]
    assert "Failing tests:" in first and "tests/test_service.py::test_filter_by_status" in first
    assert "--- pytest output (end) ---" not in first and "<untrusted_repo_content" in first
    assert len(first) < 3_000
