"""LangSmith tracing for RepoPilot.

Tracing is OFF unless the environment says otherwise (LangSmith's own variables):
    LANGSMITH_TRACING=true
    LANGSMITH_API_KEY=lsv2_...
    LANGSMITH_PROJECT=repopilot          (optional, default "default")
    LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com   (optional, EU region)

When it is on, every agent run appears in LangSmith as one trace per graph invocation
(start, then one per human decision), grouped into a thread by `thread_id`:
    repopilot (graph) -> plan / tools / human_review / apply_write / finish (nodes)
                      -> llm.complete (run_type=llm, with token usage) and one run per tool call
When it is off, the decorators below are no-ops and nothing leaves the machine.

Privacy: traces contain repository source code, test output and the bug report. They are
sent to LangSmith, a third-party service. Never enable tracing on a private codebase you
are not allowed to share.
"""
from __future__ import annotations

import os
from typing import Any

from langsmith import traceable
from langsmith import utils as ls_utils

from agent.llm import LLMClient, LLMResponse, to_openai_messages
from agent.schemas import ToolResult


def tracing_enabled() -> bool:
    return bool(ls_utils.tracing_is_enabled())


def tracing_project() -> str:
    return os.environ.get("LANGSMITH_PROJECT") or os.environ.get("LANGCHAIN_PROJECT") or "default"


def flush() -> None:
    """Wait until queued traces are sent (call before a CLI process exits)."""
    if not tracing_enabled():
        return
    try:
        from langchain_core.tracers.langchain import wait_for_all_tracers

        wait_for_all_tracers()
    except Exception:  # noqa: BLE001 - tracing must never break the agent
        pass


# --- LLM calls -------------------------------------------------------------------------------
def _llm_inputs(inputs: dict) -> dict:
    return {
        "messages": to_openai_messages(inputs.get("system", ""), inputs.get("messages") or []),
        "tools": [t["name"] for t in inputs.get("tools") or []],
    }


def _llm_outputs(response: Any) -> dict:
    if not isinstance(response, LLMResponse):
        return {"output": response}
    return {
        "role": "assistant",
        "content": response.text,
        "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                       for c in response.tool_calls],
        "usage_metadata": {
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "total_tokens": response.input_tokens + response.output_tokens,
            "input_token_details": {"cache_read": response.cached_input_tokens},
        },
        "usage_estimated": response.usage_estimated,
    }


@traceable(run_type="llm", name="llm.complete", process_inputs=_llm_inputs, process_outputs=_llm_outputs)
def _traced_complete(llm: LLMClient, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
    return llm.complete(system, messages, tools)


def complete(llm: LLMClient, system: str, messages: list[dict], tools: list[dict], *, step: int) -> LLMResponse:
    """llm.complete(), recorded in LangSmith as an LLM run with its token usage."""
    return _traced_complete(llm, system, messages, tools,
                            langsmith_extra={"metadata": {"ls_model_name": llm.name, "step": step}})


# --- tool calls ------------------------------------------------------------------------------
def _tool_inputs(inputs: dict) -> dict:
    return {"tool": inputs.get("name"), "arguments": inputs.get("args"), "actor": inputs.get("actor")}


def _tool_outputs(result: Any) -> dict:
    if not isinstance(result, ToolResult):
        return {"output": result}
    return {"ok": result.ok, "output": result.output, "error": result.error, "data": result.data}


@traceable(run_type="tool", process_inputs=_tool_inputs, process_outputs=_tool_outputs)
def _traced_tool(executor: Any, name: str, args: Any, actor: str, step: Any) -> ToolResult:
    return executor.call(name, args, actor=actor, step=step)


def call_tool(executor: Any, name: str, args: Any, *, actor: str, step: Any = None) -> ToolResult:
    """executor.call(), recorded in LangSmith as a tool run named after the tool."""
    return _traced_tool(executor, name, args, actor, step,
                        langsmith_extra={"name": name or "unknown_tool", "metadata": {"actor": actor, "step": step}})
