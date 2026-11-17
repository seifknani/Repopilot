"""Errors raised by tools. Their message is shown verbatim to the model, so it must be
short, specific and say what to do next. It is never a stack trace."""


class ToolError(Exception):
    """A tool could not do what was asked (bad path, missing file, not approved, ...)."""


class PolicyViolation(ToolError):
    """The request was refused by a safety rule (path allow-list, branch guard, ...)."""
