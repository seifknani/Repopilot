"""LLM interface used by the loop, plus a deterministic scripted LLM for tests and evaluation.

The loop only depends on `LLMClient.complete()`. A real provider adapter (step 10+) has to
translate the neutral message format below to and from its own API, and report token usage.

Neutral message format:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments", "extra"?}]}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Union


class LLMError(Exception):
    """The model provider failed (network, rate limit, invalid response...)."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable  # False: retrying cannot help (no credits, bad key) - stop at once


NON_RETRYABLE_CODES = ("insufficient_quota", "invalid_api_key", "billing_hard_limit_reached", "API_KEY_INVALID")


@dataclass
class ToolCall:
    name: str
    arguments: Any = field(default_factory=dict)
    id: str = ""
    # Provider data that must be sent back unchanged with this call on the next turn. Gemini 3 puts
    # its "thought signature" here (extra_content.google.thought_signature) and rejects a follow-up
    # request that drops it (HTTP 400).
    extra: Optional[dict] = None


@dataclass
class LLMResponse:
    text: str = ""  # the model's plan / explanation for this step
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    usage_estimated: bool = False
    cached_input_tokens: int = 0  # part of input_tokens served from the provider's prompt cache
    reasoning_tokens: int = 0  # part of output_tokens spent on hidden reasoning (billed as output)


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
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-5-mini"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_DEFAULT_MODEL = "gemini-3.5-flash-lite"

# USD per million tokens: (input, cached input, output), standard tier.
# Source: https://developers.openai.com/api/docs/pricing (checked 2026-11-18).
# Flex tier (service_tier="flex") is half price for the models that support it.
MODEL_PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    # Gemini, paid tier (free tier: $0 within its rate limits). Source: https://ai.google.dev/gemini-api/docs/pricing
    # (checked 2026-11-24). Cached input set to the full input price (conservative: implicit caching is not
    # reported reliably through the OpenAI-compatible endpoint).
    "gemini-3.5-flash-lite": (0.30, 0.30, 2.50),
    "gemini-3.6-flash": (1.50, 1.50, 7.50),
}
FLEX_MODELS = frozenset({"gpt-5-nano", "gpt-5-mini", "gpt-5.4-mini", "gpt-5.6-luna"})


def is_reasoning_model(model: str) -> bool:
    """gpt-5* and o-series models reject a custom temperature and spend output tokens on reasoning."""
    name = model.split("/")[-1]
    return name.startswith(("gpt-5", "o1", "o3", "o4"))


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
                     "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], default=str)},
                     **({"extra_content": c["extra"]} if c.get("extra") else {})}
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

    def __init__(self, model: str, api_key: str, base_url: str = DEFAULT_BASE_URL, *,
                 temperature: Optional[float] = 0.0, max_output_tokens: int = 2048,
                 reasoning_effort: Optional[str] = None, service_tier: Optional[str] = None,
                 prompt_cache_key: Optional[str] = None, timeout_s: float = 90.0, rate_limit_retries: int = 4,
                 max_wait_s: float = 60.0, transport: Optional[httpx.BaseTransport] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not api_key:
            raise LLMError("No API key configured for the LLM provider.")
        self.name = model
        self.temperature = temperature  # None = provider default (required by reasoning models)
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort  # "minimal" | "low" | "medium" | "high" (reasoning models)
        self.service_tier = service_tier  # "flex" = half price, slower (OpenAI)
        self.prompt_cache_key = prompt_cache_key  # same key + same prefix -> more cache hits (OpenAI)
        self.rate_limit_retries = rate_limit_retries
        self.max_wait_s = max_wait_s
        self.sleep = sleep
        self.rate_limit_waits = 0  # how often we waited for a 429 (free-tier rate limits)
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport,
                                   headers={"Authorization": f"Bearer {api_key}"})

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        # The prefix (system prompt + tool schemas + earlier messages) never changes during a run, so
        # providers with prompt caching bill it at the cached-input rate from the second call on.
        payload: dict[str, Any] = {
            "model": self.name, "max_completion_tokens": self.max_output_tokens,
            "messages": to_openai_messages(system, messages), "tools": to_openai_tools(tools), "tool_choice": "auto",
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if self.service_tier:
            payload["service_tier"] = self.service_tier
        if self.prompt_cache_key:
            payload["prompt_cache_key"] = self.prompt_cache_key
        for attempt in range(self.rate_limit_retries + 1):
            try:
                response = self.client.post("/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                raise LLMError(f"Network error calling the LLM: {type(exc).__name__}") from exc
            if response.status_code in (401, 429) and any(c in response.text for c in NON_RETRYABLE_CODES):
                # No credits / spending limit / bad key: waiting will not help, and retries cost time.
                raise LLMError(f"LLM provider refused the request (HTTP {response.status_code}, account problem): "
                               f"{response.text[:300]}", retryable=False)
            if response.status_code in (400, 401, 403) and "API_KEY_INVALID" in response.text:
                raise LLMError("LLM provider rejected the API key.", retryable=False)
            if response.status_code in (429, 500, 502, 503) and attempt < self.rate_limit_retries:
                # Free tiers limit tokens per minute (429); Gemini answers 503 "high demand" at peaks:
                # wait as instructed, then retry the same request.
                try:
                    wait = float(response.headers.get("retry-after", "5"))
                except ValueError:
                    wait = 5.0
                self.rate_limit_waits += 1
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
                                  id=tc.get("id") or f"call_{i}", extra=tc.get("extra_content") or None))
        usage = body.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        # Gemini leaves its thinking tokens out of completion_tokens (they only appear in total_tokens),
        # but bills them as output: count them, so the cost and the stop rule see the real usage.
        hidden = int(usage.get("total_tokens") or 0) - prompt_tokens - output_tokens
        if hidden > 0:
            output_tokens += hidden
            reasoning += hidden
        return LLMResponse(
            text=message.get("content") or "",
            tool_calls=calls,
            input_tokens=prompt_tokens,
            output_tokens=output_tokens,
            usage_estimated=not usage,
            cached_input_tokens=int(cached),
            reasoning_tokens=int(reasoning),
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


def pricing_for(model: str, service_tier: Optional[str] = None):
    """Pricing from REPOPILOT_LLM_PRICE_* if set, else from MODEL_PRICES (halved for flex)."""
    from agent.stop_rule import Pricing  # local import: stop_rule does not depend on llm

    known = MODEL_PRICES.get(model.split("/")[-1])
    env_in, env_out = os.environ.get("REPOPILOT_LLM_PRICE_IN"), os.environ.get("REPOPILOT_LLM_PRICE_OUT")
    env_cached = os.environ.get("REPOPILOT_LLM_PRICE_CACHED")
    if env_in or env_out:
        price_in, price_out = float(env_in or 0), float(env_out or 0)
        price_cached = float(env_cached) if env_cached else price_in
    elif known:
        price_in, price_cached, price_out = known
        if service_tier == "flex":
            price_in, price_cached, price_out = price_in / 2, price_cached / 2, price_out / 2
    else:
        price_in = price_cached = price_out = 0.0
    return Pricing(model=model, input_per_mtok=price_in, output_per_mtok=price_out,
                   cached_input_per_mtok=price_cached)


def llm_from_env(project_root: Optional[Path] = None):
    """Build the real LLM and its Pricing from environment variables (or a git-ignored .env).

    Key: REPOPILOT_LLM_API_KEY, else GEMINI_API_KEY (Gemini defaults), else OPENAI_API_KEY (OpenAI
    defaults), else GROQ_API_KEY (Groq defaults).
    Optional: REPOPILOT_LLM_MODEL, REPOPILOT_LLM_BASE_URL, REPOPILOT_LLM_REASONING_EFFORT (default "low"
    for reasoning models on OpenAI), REPOPILOT_LLM_SERVICE_TIER ("flex" = half price on OpenAI),
    REPOPILOT_LLM_MAX_OUTPUT_TOKENS (default 2048), REPOPILOT_LLM_TEMPERATURE,
    REPOPILOT_LLM_PRICE_IN / _CACHED / _OUT (USD per million tokens; default: MODEL_PRICES).
    Returns (None, None) when no key is configured.
    """
    if project_root is not None:
        load_dotenv(project_root / ".env")
    env = os.environ
    if env.get("REPOPILOT_LLM_API_KEY"):
        key, base, model = env["REPOPILOT_LLM_API_KEY"], DEFAULT_BASE_URL, DEFAULT_MODEL
    elif env.get("GEMINI_API_KEY"):
        key, base, model = env["GEMINI_API_KEY"], GEMINI_BASE_URL, GEMINI_DEFAULT_MODEL
    elif env.get("OPENAI_API_KEY"):
        key, base, model = env["OPENAI_API_KEY"], OPENAI_BASE_URL, OPENAI_DEFAULT_MODEL
    elif env.get("GROQ_API_KEY"):
        key, base, model = env["GROQ_API_KEY"], DEFAULT_BASE_URL, DEFAULT_MODEL
    else:
        return None, None
    model = env.get("REPOPILOT_LLM_MODEL") or model
    base = env.get("REPOPILOT_LLM_BASE_URL") or base
    is_openai = "api.openai.com" in base
    is_gemini = "generativelanguage.googleapis.com" in base
    reasoning = is_reasoning_model(model)
    temperature_env = env.get("REPOPILOT_LLM_TEMPERATURE")
    # Gemini 3 models are tuned for their default temperature (Google advises against lowering it).
    temperature = float(temperature_env) if temperature_env else (None if reasoning or is_gemini else 0.0)
    effort = env.get("REPOPILOT_LLM_REASONING_EFFORT") or ("low" if (reasoning and is_openai) or is_gemini else None)
    tier = (env.get("REPOPILOT_LLM_SERVICE_TIER") or None) if is_openai else None
    if tier == "flex" and model.split("/")[-1] not in FLEX_MODELS:
        tier = None  # flex is not offered for this model: use the standard tier
    llm = OpenAICompatibleLLM(
        model, key, base, temperature=temperature,
        max_output_tokens=int(env.get("REPOPILOT_LLM_MAX_OUTPUT_TOKENS") or 2048),
        reasoning_effort=effort, service_tier=tier,
        prompt_cache_key="repopilot-v1" if is_openai else None,
        timeout_s=600.0 if tier == "flex" else 120.0,  # flex requests can wait in a queue
    )
    return llm, pricing_for(model, tier)
