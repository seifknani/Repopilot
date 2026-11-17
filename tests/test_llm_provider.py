"""The real-provider adapter, tested offline with an HTTP mock (no key, no network, no cost)."""
import json

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
    for var in ("REPOPILOT_LLM_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert llm_from_env(tmp_path) == (None, None)


def test_env_file_configures_model_and_price(tmp_path, monkeypatch):
    for var in ("REPOPILOT_LLM_API_KEY", "GROQ_API_KEY", "REPOPILOT_LLM_MODEL", "REPOPILOT_LLM_PRICE_IN",
                "REPOPILOT_LLM_PRICE_OUT", "REPOPILOT_LLM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text("GROQ_API_KEY=abc\nREPOPILOT_LLM_MODEL=some/model\nREPOPILOT_LLM_PRICE_IN=0.5\n"
                                   "REPOPILOT_LLM_PRICE_OUT=2\n")
    llm, pricing = llm_from_env(tmp_path)
    assert llm.name == "some/model" and pricing.cost(1_000_000, 1_000_000) == 2.5
    assert to_openai_messages("s", [])[0]["role"] == "system"
