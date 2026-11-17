"""Single entry point for every tool call: validate -> run -> convert errors -> log.

Whatever happens, the caller gets a ToolResult. Failures become a short message the model
can act on, never a stack trace, and every call is logged with its input, output and duration.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from pydantic import ValidationError

from agent.approvals import ApprovalStore
from agent.config import AgentConfig
from agent.errors import PolicyViolation, ToolError
from agent.injection import scan
from agent.policy import Policy
from agent.schemas import ToolResult
from agent.tools import TOOLS
from agent.tools.base import ToolContext
from agent.trajectory_log import TrajectoryLogger


def format_validation_error(tool: str, exc: ValidationError) -> str:
    problems = []
    for err in exc.errors():
        where = ".".join(str(part) for part in err["loc"]) or "arguments"
        message = "unexpected argument (not part of this tool's schema)" if err["type"] == "extra_forbidden" else err["msg"]
        problems.append(f"{where}: {message}")
    return f"Invalid arguments for {tool}: " + "; ".join(problems)


class ToolExecutor:
    def __init__(
        self,
        config: AgentConfig,
        approvals: Optional[ApprovalStore] = None,
        logger: Optional[TrajectoryLogger] = None,
    ) -> None:
        self.config = config
        self.approvals = approvals or ApprovalStore()
        self.logger = logger or TrajectoryLogger(config.log_path)
        self.ctx = ToolContext(config=config, policy=Policy(config), approvals=self.approvals)

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Name, description and JSON schema of every operation (for the LLM in step 10)."""
        return [
            {"name": spec.name, "description": spec.description, "input_schema": spec.input_model.model_json_schema()}
            for spec in TOOLS.values()
        ]

    def call(self, name: str, args: Optional[dict[str, Any]] = None, *, actor: str = "agent",
             step: Optional[int] = None) -> ToolResult:
        start = time.perf_counter()
        data: dict[str, Any] = {}
        output, error, untrusted, ok = "", None, False, False
        spec = TOOLS.get(name)
        try:
            if spec is None:
                raise ToolError(f"Unknown tool '{name}'. Available tools: {', '.join(TOOLS)}.")
            if args is not None and not isinstance(args, dict):
                raise ToolError(f"Arguments for {name} must be a JSON object.")
            parsed = spec.input_model.model_validate(args or {})
            result = spec.func(self.ctx, parsed)
            output, data, untrusted, ok = result.output, result.data, result.untrusted, True
            if untrusted:
                findings = scan(output)
                if findings:
                    data = {**data, "injection_flags": findings}
        except ValidationError as exc:
            error = format_validation_error(name, exc)
            data = {"error_type": "invalid_arguments"}
        except PolicyViolation as exc:
            error = str(exc)
            data = {"error_type": "policy_violation"}
        except ToolError as exc:
            error = str(exc)
            data = {"error_type": "tool_error"}
        except Exception as exc:  # noqa: BLE001 - last line of defence: never leak a traceback
            error = f"Internal error in {name} ({type(exc).__name__}). The operation did not complete; try another approach."
            data = {"error_type": "internal_error", "exception": f"{type(exc).__name__}: {exc}"}

        limit = self.config.max_output_chars
        if len(output) > limit:
            output = output[:limit] + f"\n...[output truncated at {limit} characters]"
            data["truncated"] = True

        result = ToolResult(
            tool=name, ok=ok, output=output, error=error, data=data, untrusted=untrusted,
            duration_ms=(time.perf_counter() - start) * 1000,
        )
        self.logger.log_tool_call(
            tool=name, args=args, ok=ok, output=output, error=error, data=data,
            duration_ms=result.duration_ms, actor=actor, step=step,
        )
        return result
