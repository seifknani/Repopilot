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
