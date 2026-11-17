"""Heuristic prompt-injection detection and risk flags for proposed edits.

These checks produce WARNINGS for the model and the human reviewer. They are not the
security boundary: a clever injection can avoid any regex. The boundary is
  1. the policy layer: what the tools can do at all (paths, editable files, branches, no shell),
  2. the human checkpoint: nothing is written, committed or pushed without approval.
"""
from __future__ import annotations

import re

# (kind, pattern) – matched case-insensitively against untrusted tool output.
INJECTION_PATTERNS: list[tuple[str, str]] = [
    ("override_instructions",
     r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|your|system)\b"
     r"[^.\n]{0,20}\b(instructions?|prompts?|rules?|directions?)\b"),
    ("role_hijack", r"(^|\n)\s*(#\s*|//\s*)?(system|assistant|developer)\s*:"),
    ("role_hijack", r"\byou are now\b|\bnew instructions\b"),
    ("auto_approve", r"\bauto(matically)?[- ]?approve|\bapprove (all|every|this|it|automatically)\b"
                     r"|\bskip (the )?(human|review|approval)"),
    ("destructive", r"\brm\s+-rf\b|\bdelete (the |all |every )?(entire |whole )?(repo|repository|files|branch|database)\b"
                    r"|\bdrop table\b|\bforce[- ]push\b|\bpush\s+--force\b"),
    ("exfiltration", r"\b(api[_ ]?keys?|passwords?|secrets?|tokens?|credentials?|\.env|id_rsa)\b[^\n]{0,40}"
                     r"\b(send|post|upload|reveal|exfiltrat\w*)\b"
                     r"|\b(send|post|upload|reveal|exfiltrat\w*)\b[^\n]{0,40}"
                     r"\b(api[_ ]?keys?|passwords?|secrets?|tokens?|credentials?|\.env|id_rsa)\b"),
    ("tool_steering", r"\b(call|use|run|execute)\s+(the\s+)?(push|commit|apply_edit|shell|bash|terminal|os\.system)\b"),
    ("tag_spoofing", r"</?untrusted_repo_content|</?bug_report"),
    ("test_tampering", r"\b(edit|modify|change|delete|skip|disable|rewrite)\b[^\n]{0,30}\btests?\b[^\n]{0,40}"
                       r"\b(pass|expect|green|500)\b"),
]
_COMPILED = [(kind, re.compile(pattern, re.IGNORECASE)) for kind, pattern in INJECTION_PATTERNS]

# Code that a bug fix in this API never needs; flagged when ADDED by an edit.
DANGEROUS_CODE: list[tuple[str, str]] = [
    ("shell command (os.system)", r"\bos\.system\s*\(|\bos\.popen\s*\("),
    ("subprocess", r"\bsubprocess\b"),
    ("eval/exec", r"\b(eval|exec)\s*\("),
    ("dynamic import", r"__import__\s*\("),
    ("file deletion", r"\bshutil\.rmtree\b|\bos\.(remove|unlink|rmdir)\b|\.unlink\s*\("),
    ("network access", r"\b(socket|requests|urllib|http\.client|httpx)\b"),
    ("unsafe deserialization", r"\bpickle\.loads?\b|\bmarshal\.loads\b"),
    ("encoded payload", r"\bbase64\.b64decode\b"),
    ("test skipping", r"pytest\.(skip|mark\.skip)|unittest\.skip"),
    ("environment / secrets access", r"\bos\.environ\b|\bgetenv\s*\("),
]
MAX_REMOVED_LINES = 8


def scan(text: str, limit: int = 10) -> list[dict[str, str]]:
    """Return instruction-like passages found in `text` (kind + the offending line)."""
    findings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kind, regex in _COMPILED:
        for match in regex.finditer(text):
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.end())
            line = text[start:end if end != -1 else len(text)].strip()[:160]
            if (kind, line) not in seen:
                seen.add((kind, line))
                findings.append({"kind": kind, "excerpt": line})
            if len(findings) >= limit:
                return findings
    return findings


def assess_edit(path: str, old_text: str, new_text: str) -> list[str]:
    """Risk flags shown to the human next to a proposed edit."""
    flags = []
    for label, pattern in DANGEROUS_CODE:
        regex = re.compile(pattern)
        if regex.search(new_text) and not regex.search(old_text):
            flags.append(f"adds {label}")
    removed = old_text.count("\n") - new_text.count("\n")
    if removed > MAX_REMOVED_LINES:
        flags.append(f"removes {removed} lines")
    if scan(new_text):
        flags.append("new code contains instruction-like text (possible injected content)")
    return flags
