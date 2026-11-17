"""LLM interface used by the loop, plus a deterministic scripted LLM for tests and evaluation.

The loop only depends on `LLMClient.complete()`. A real provider adapter (step 10+) has to
translate the neutral message format below to and from its own API, and report token usage.

Neutral message format:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments"}]}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Union


class LLMError(Exception):
    """The model provider failed (network, rate limit, invalid response...)."""


@dataclass
class ToolCall:
    name: str
    arguments: Any = field(default_factory=dict)
    id: str = ""


@dataclass
class LLMResponse:
    text: str = ""  # the model's plan / explanation for this step
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    usage_estimated: bool = False


class LLMClient(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse: ...


def estimate_tokens(text: str) -> int:
    """Rough token count (about 4 characters per token), used when no real usage is reported."""
    return max(1, len(text) // 4)


ScriptItem = Union[LLMResponse, Exception, Callable[[list[dict]], Union[LLMResponse, Exception]]]


class ScriptedLLM:
    """Plays back a fixed list of responses, in order. Fully deterministic, no network, no cost.

    Each item is an LLMResponse, an Exception (raised, to simulate a provider failure), or a
    callable that receives the conversation so far and returns either of those.
    When the script runs out: `repeat_last=True` keeps replaying the last item (an endless
    agent, used to prove the stop rule). Otherwise it returns a final answer with no tool call.
    """

    def __init__(self, script: list[ScriptItem], *, repeat_last: bool = False, name: str = "scripted") -> None:
        if not script:
            raise ValueError("script must not be empty")
        self.script = list(script)
        self.repeat_last = repeat_last
        self.name = name
        self.calls = 0
        self.seen_tools: list[str] = []
        self.seen_messages: list[list[dict]] = []

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        index = self.calls
        self.calls += 1
        self.seen_tools = [t["name"] for t in tools]
        self.seen_messages.append([dict(m) for m in messages])
        if index < len(self.script):
            item = self.script[index]
        elif self.repeat_last:
            item = self.script[-1]
        else:
            item = LLMResponse(text="Script finished. Stopping.")
        if callable(item) and not isinstance(item, LLMResponse):
            item = item(messages)
        if isinstance(item, Exception):
            raise item
        calls = [ToolCall(c.name, c.arguments, c.id or f"call_{self.calls}_{i}") for i, c in enumerate(item.tool_calls)]
        prompt_text = system + json.dumps(messages, default=str)
        output_text = item.text + json.dumps([[c.name, c.arguments] for c in calls], default=str)
        return LLMResponse(
            text=item.text,
            tool_calls=calls,
            input_tokens=item.input_tokens or estimate_tokens(prompt_text),
            output_tokens=item.output_tokens or estimate_tokens(output_text),
            usage_estimated=not (item.input_tokens or item.output_tokens),
        )


def say(text: str, *calls: ToolCall, input_tokens: int = 0, output_tokens: int = 0) -> LLMResponse:
    """Build one scripted response: an explanation plus zero or more tool calls."""
    return LLMResponse(text=text, tool_calls=list(calls), input_tokens=input_tokens, output_tokens=output_tokens)


def call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(name=name, arguments=arguments)


def find_last_tool_result(messages: list[dict], name: Optional[str] = None) -> Optional[str]:
    for message in reversed(messages):
        if message.get("role") == "tool" and (name is None or message.get("name") == name):
            return message["content"]
    return None


# --- real provider: any OpenAI-compatible chat-completions API (default: Groq) ------------------
import os  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Translate the neutral message format into OpenAI chat-completions messages."""
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg: dict = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], default=str)}}
                    for c in m["tool_calls"]
                ]
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
    return out


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools]


class OpenAICompatibleLLM:
    """Calls a chat-completions endpoint with tool calling and reports the real token usage."""

    def __init__(self, model: str, api_key: str, base_url: str = DEFAULT_BASE_URL, *, temperature: float = 0.0,
                 max_output_tokens: int = 2048, timeout_s: float = 90.0, rate_limit_retries: int = 4,
                 max_wait_s: float = 60.0, transport: Optional[httpx.BaseTransport] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not api_key:
            raise LLMError("No API key configured for the LLM provider.")
        self.name = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.rate_limit_retries = rate_limit_retries
        self.max_wait_s = max_wait_s
        self.sleep = sleep
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport,
                                   headers={"Authorization": f"Bearer {api_key}"})

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        payload = {
            "model": self.name, "temperature": self.temperature, "max_tokens": self.max_output_tokens,
            "messages": to_openai_messages(system, messages), "tools": to_openai_tools(tools), "tool_choice": "auto",
        }
        for attempt in range(self.rate_limit_retries + 1):
            try:
                response = self.client.post("/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                raise LLMError(f"Network error calling the LLM: {type(exc).__name__}") from exc
            if response.status_code == 429 and attempt < self.rate_limit_retries:
                # Free tiers limit tokens per minute: wait as instructed, then retry the same request.
                try:
                    wait = float(response.headers.get("retry-after", "5"))
                except ValueError:
                    wait = 5.0
                self.sleep(min(max(wait, 1.0), self.max_wait_s))
                continue
            if response.status_code != 200:
                raise LLMError(f"LLM provider returned HTTP {response.status_code}: {response.text[:200]}")
            break
        try:
            body = response.json()
            message = body["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError("The LLM provider returned an unexpected response.") from exc

        calls = []
        for i, tc in enumerate(message.get("tool_calls") or []):
            raw = (tc.get("function") or {}).get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = raw  # the executor answers "Arguments ... must be a JSON object"
            calls.append(ToolCall(name=(tc.get("function") or {}).get("name", ""), arguments=args,
                                  id=tc.get("id") or f"call_{i}"))
        usage = body.get("usage") or {}
        return LLMResponse(
            text=message.get("content") or "",
            tool_calls=calls,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            usage_estimated=not usage,
        )


def load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE lines); existing environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def llm_from_env(project_root: Optional[Path] = None):
    """Build the real LLM and its Pricing from environment variables (or a git-ignored .env).

    REPOPILOT_LLM_API_KEY (or GROQ_API_KEY), REPOPILOT_LLM_MODEL, REPOPILOT_LLM_BASE_URL,
    REPOPILOT_LLM_PRICE_IN / REPOPILOT_LLM_PRICE_OUT (USD per million tokens).
    Returns (None, None) when no key is configured.
    """
    from agent.stop_rule import Pricing  # local import: stop_rule does not depend on llm

    if project_root is not None:
        load_dotenv(project_root / ".env")
    key = os.environ.get("REPOPILOT_LLM_API_KEY") or os.environ.get("GROQ_API_KEY")
    if not key:
        return None, None
    model = os.environ.get("REPOPILOT_LLM_MODEL", DEFAULT_MODEL)
    pricing = Pricing(model=model,
                      input_per_mtok=float(os.environ.get("REPOPILOT_LLM_PRICE_IN", "0")),
                      output_per_mtok=float(os.environ.get("REPOPILOT_LLM_PRICE_OUT", "0")))
    llm = OpenAICompatibleLLM(model, key, os.environ.get("REPOPILOT_LLM_BASE_URL", DEFAULT_BASE_URL))
    return llm, pricing
