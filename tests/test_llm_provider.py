"""The real-provider adapter, tested offline with an HTTP mock (no key, no network, no cost)."""
import json
import os

import httpx
import pytest

from agent.llm import LLMError, OpenAICompatibleLLM, llm_from_env, to_openai_messages

TOOLS = [{"name": "read_file", "description": "Read a file", "input_schema": {"type": "object", "properties": {}}}]


def reply(message, usage=None, status=200, headers=None):
    body = {"choices": [{"message": message}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(status, json=body, headers=headers or {})


def make(handler, **kw):
    return OpenAICompatibleLLM("test-model", "k", "https://llm.invalid/v1", transport=httpx.MockTransport(handler),
                               sleep=lambda s: None, **kw)


def test_request_translation_and_tool_call_parsing():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content), auth=request.headers["authorization"], url=str(request.url))
        return reply({"content": "Reading.", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "app/main.py"}'}}]},
            usage={"prompt_tokens": 1200, "completion_tokens": 40})

    messages = [{"role": "user", "content": "bug"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c0", "name": "list_files", "arguments": {}}]},
                {"role": "tool", "tool_call_id": "c0", "name": "list_files", "content": "app/"}]
    r = make(handler).complete("SYSTEM", messages, TOOLS)
    assert seen["url"] == "https://llm.invalid/v1/chat/completions" and seen["auth"] == "Bearer k"
    assert seen["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert seen["messages"][2]["tool_calls"][0]["function"] == {"name": "list_files", "arguments": "{}"}
    assert seen["messages"][3] == {"role": "tool", "tool_call_id": "c0", "content": "app/"}
    assert seen["tools"][0]["function"]["name"] == "read_file" and seen["temperature"] == 0
    assert r.text == "Reading." and r.tool_calls[0].name == "read_file" and r.tool_calls[0].arguments == {"path": "app/main.py"}
    assert (r.input_tokens, r.output_tokens, r.usage_estimated) == (1200, 40, False)


def test_malformed_arguments_are_passed_on_for_a_clear_tool_error():
    r = make(lambda req: reply({"content": None, "tool_calls": [
        {"id": "c1", "function": {"name": "read_file", "arguments": "{not json"}}]})).complete("s", [], TOOLS)
    assert r.tool_calls[0].arguments == "{not json" and r.usage_estimated


def test_rate_limit_is_waited_out_then_retried():
    answers = iter([httpx.Response(429, headers={"retry-after": "2"}), reply({"content": "ok"}, usage={})])
    waits = []
    llm = OpenAICompatibleLLM("m", "k", "https://llm.invalid/v1", sleep=waits.append,
                              transport=httpx.MockTransport(lambda req: next(answers)))
    assert llm.complete("s", [], TOOLS).text == "ok" and waits == [2.0]


@pytest.mark.parametrize("response", [httpx.Response(500, text="boom"), httpx.Response(200, json={"nope": 1}),
                                      httpx.Response(429)])
def test_provider_failures_become_llm_errors(response):
    with pytest.raises(LLMError):
        make(lambda req: response, rate_limit_retries=0).complete("s", [], TOOLS)


def test_no_key_means_no_real_llm(tmp_path, monkeypatch):
    for var in ("REPOPILOT_LLM_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert llm_from_env(tmp_path) == (None, None)


def test_env_file_configures_model_and_price(tmp_path, monkeypatch):
    for var in ("REPOPILOT_LLM_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "REPOPILOT_LLM_MODEL", "REPOPILOT_LLM_PRICE_IN",
                "REPOPILOT_LLM_PRICE_OUT", "REPOPILOT_LLM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text("GROQ_API_KEY=abc\nREPOPILOT_LLM_MODEL=some/model\nREPOPILOT_LLM_PRICE_IN=0.5\n"
                                   "REPOPILOT_LLM_PRICE_OUT=2\n")
    llm, pricing = llm_from_env(tmp_path)
    assert llm.name == "some/model" and pricing.cost(1_000_000, 1_000_000) == 2.5
    assert to_openai_messages("s", [])[0]["role"] == "system"


# --- OpenAI / cost optimisation ------------------------------------------------------------------
LLM_ENV = ("REPOPILOT_LLM_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "REPOPILOT_LLM_MODEL", "REPOPILOT_LLM_BASE_URL",
           "REPOPILOT_LLM_PRICE_IN", "REPOPILOT_LLM_PRICE_OUT", "REPOPILOT_LLM_PRICE_CACHED",
           "REPOPILOT_LLM_REASONING_EFFORT", "REPOPILOT_LLM_SERVICE_TIER", "REPOPILOT_LLM_TEMPERATURE",
           "REPOPILOT_LLM_MAX_OUTPUT_TOKENS")


@pytest.fixture
def clean_env(monkeypatch):
    for var in LLM_ENV:
        monkeypatch.delenv(var, raising=False)
    yield
    for var in LLM_ENV:  # load_dotenv writes os.environ directly: never leak a fake key into later tests
        os.environ.pop(var, None)


def test_openai_key_selects_cheap_reasoning_defaults(tmp_path, clean_env):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test\n")
    llm, pricing = llm_from_env(tmp_path)
    assert llm.name == "gpt-5-mini" and str(llm.client.base_url).startswith("https://api.openai.com/v1")
    assert llm.temperature is None and llm.reasoning_effort == "low" and llm.service_tier is None
    assert llm.prompt_cache_key == "repopilot-v1"
    assert (pricing.input_per_mtok, pricing.cached_input_per_mtok, pricing.output_per_mtok) == (0.25, 0.025, 2.00)


def test_flex_tier_halves_the_price_and_is_dropped_where_unsupported(tmp_path, clean_env):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test\nREPOPILOT_LLM_SERVICE_TIER=flex\n")
    llm, pricing = llm_from_env(tmp_path)
    assert llm.service_tier == "flex" and pricing.output_per_mtok == 1.00
    for var in LLM_ENV:
        os.environ.pop(var, None)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test\nREPOPILOT_LLM_SERVICE_TIER=flex\n"
                                   "REPOPILOT_LLM_MODEL=gpt-4o-mini\n")
    llm, pricing = llm_from_env(tmp_path)
    assert llm.service_tier is None and llm.temperature == 0.0 and pricing.output_per_mtok == 0.60


def test_reasoning_request_shape():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return reply({"content": "ok"}, usage={"prompt_tokens": 2000, "completion_tokens": 300,
                                               "prompt_tokens_details": {"cached_tokens": 1536},
                                               "completion_tokens_details": {"reasoning_tokens": 256}})

    r = make(handler, temperature=None, reasoning_effort="low", service_tier="flex",
             prompt_cache_key="k").complete("s", [], TOOLS)
    assert "temperature" not in seen and "max_tokens" not in seen and seen["max_completion_tokens"] == 2048
    assert (seen["reasoning_effort"], seen["service_tier"], seen["prompt_cache_key"]) == ("low", "flex", "k")
    assert (r.cached_input_tokens, r.reasoning_tokens) == (1536, 256)


def test_no_credits_fails_fast_without_waiting():
    waits = []
    body = {"error": {"message": "You have no credits remaining.", "type": "insufficient_quota"}}
    llm = OpenAICompatibleLLM("m", "k", "https://llm.invalid/v1", sleep=waits.append,
                              transport=httpx.MockTransport(lambda req: httpx.Response(429, json=body)))
    with pytest.raises(LLMError) as err:
        llm.complete("s", [], TOOLS)
    assert err.value.retryable is False and waits == []


def test_loop_does_not_retry_a_non_retryable_error(executor):
    from agent.llm import ScriptedLLM
    from agent.loop import Agent
    from agent.stop_rule import StopReason

    result = Agent(executor, ScriptedLLM([LLMError("no credits", retryable=False)], repeat_last=True),
                   baseline_tests=False).start("x")
    assert result.stop_reason == StopReason.LLM_ERROR
    assert sum(r["type"] == "llm_error" for r in executor.logger.records) == 1


def test_no_credits_is_explained_to_the_user(executor):
    from agent.llm import ScriptedLLM
    from agent.loop import Agent

    err = LLMError('HTTP 429: {"type": "insufficient_quota"}', retryable=False)
    result = Agent(executor, ScriptedLLM([err], repeat_last=True), baseline_tests=False).start("x")
    assert "no credits left" in result.final_message


# --- Gemini (OpenAI-compatible endpoint) -------------------------------------------------------
def test_gemini_key_selects_gemini_defaults_and_wins_over_openai(tmp_path, clean_env):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test\nGEMINI_API_KEY=g-test\n")
    llm, pricing = llm_from_env(tmp_path)
    assert llm.name == "gemini-3.5-flash-lite"
    assert str(llm.client.base_url).startswith("https://generativelanguage.googleapis.com/v1beta/openai")
    assert llm.temperature is None and llm.reasoning_effort == "low"
    assert llm.service_tier is None and llm.prompt_cache_key is None  # OpenAI-only options are not sent
    assert (pricing.input_per_mtok, pricing.output_per_mtok) == (0.30, 2.50)


def test_gemini_thought_signature_is_sent_back_with_the_tool_call(executor):
    """Gemini 3 rejects a follow-up request whose tool call lost its thought signature (HTTP 400)."""
    from agent.loop import Agent
    from agent.stop_rule import Pricing

    signature = {"google": {"thought_signature": "c2lnbmF0dXJl"}}
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply({"content": None, "tool_calls": [{"id": "c1", "type": "function", "extra_content": signature,
                                                           "function": {"name": "git_status", "arguments": "{}"}}]},
                         usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 150})
        sent = [m for m in body["messages"] if m["role"] == "assistant"][0]["tool_calls"][0]
        if sent.get("extra_content") != signature:
            return httpx.Response(400, json={"error": {"message": "Function call is missing a thought_signature"}})
        return reply({"content": "Nothing to fix."}, usage={"prompt_tokens": 200, "completion_tokens": 5})

    result = Agent(executor, make(handler, temperature=None), pricing=Pricing(), baseline_tests=False).start("x")
    assert result.final_message == "Nothing to fix." and len(requests) == 2


def test_gemini_hidden_thinking_tokens_count_as_output():
    r = make(lambda req: reply({"content": "ok"}, usage={"prompt_tokens": 51, "completion_tokens": 20,
                                                         "total_tokens": 113})).complete("s", [], TOOLS)
    assert (r.input_tokens, r.output_tokens, r.reasoning_tokens) == (51, 62, 42)


def test_high_demand_503_is_waited_out_then_retried():
    answers = iter([httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}}), reply({"content": "ok"}, usage={})])
    waits = []
    llm = OpenAICompatibleLLM("m", "k", "https://llm.invalid/v1", sleep=waits.append,
                              transport=httpx.MockTransport(lambda req: next(answers)))
    assert llm.complete("s", [], TOOLS).text == "ok" and waits == [5.0]


def test_invalid_gemini_key_is_not_retried():
    body = [{"error": {"code": 400, "message": "API key not valid.", "status": "INVALID_ARGUMENT",
                       "details": [{"reason": "API_KEY_INVALID"}]}}]
    with pytest.raises(LLMError) as err:
        make(lambda req: httpx.Response(400, json=body)).complete("s", [], TOOLS)
    assert err.value.retryable is False
