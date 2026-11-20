"""Issue #1625: say why the nightly auto-diagnosis could not run.

The Claude action hides Claude's output (this repo is public), so a failed run only said "is_error:true". This script
reads the action's execution file and takes out JUST the final error message -- never the transcript -- redacts
anything token-like, names the kind of failure and what to do, appends that to the job summary, and prints the
comment for the nightly-failure issue on stdout.

Inputs (environment): EXEC_FILE (the action's ``execution_file`` output; falls back to
``$RUNNER_TEMP/claude-execution-output.json``), ISSUE, RUN_URL, GITHUB_STEP_SUMMARY. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_TOKEN_LIKE = re.compile(r"sk-ant-[A-Za-z0-9_-]+|[A-Za-z0-9_-]{40,}")

# (kind, words that identify it in the error text or result subtype, what the owner should do)
_KINDS = (
    (
        "authentication",
        ("invalid api key", "/login", "oauth", "unauthorized", "401", "authentication", "expired", "revoked"),
        "The CLAUDE_CODE_OAUTH_TOKEN repository secret is missing, expired or revoked. Run `claude setup-token` "
        "locally and replace the secret (Settings -> Secrets and variables -> Actions), then re-run this diagnosis "
        "from Actions -> Nightly failure auto-diagnosis -> Run workflow with this issue number.",
    ),
    (
        "usage limit",
        ("usage limit", "rate limit", "429", "limit reached", "hit your"),
        "The Claude subscription behind the token hit its usage limit. Re-run this diagnosis after the limit resets.",
    ),
    (
        "API unavailable",
        ("overloaded", "529", "service unavailable", "503"),
        "The Claude API was overloaded or unavailable. Re-run this diagnosis later.",
    ),
    (
        "turn limit",
        ("max_turns", "max turns"),
        "The diagnosis used all its turns without finishing. Read the run, then diagnose by hand or re-run.",
    ),
)
_UNKNOWN_TODO = "Read the run log; if it says nothing more, re-run the job in debug mode."


def load_messages(path: Path) -> list[dict]:
    """The execution file as a list of messages: one JSON array, or one JSON message per line."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(text)
        items = data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        items = []
        for line in text.splitlines():
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return [m for m in items if isinstance(m, dict)]


def redact(text: str, limit: int = 400) -> str:
    return " ".join(_TOKEN_LIKE.sub("[redacted]", text).split())[:limit]


def summarize(messages: list[dict]) -> dict[str, str]:
    """kind, error message (redacted), result subtype, turns and cost of the last result message."""
    result = next((m for m in reversed(messages) if m.get("type") == "result"), {})
    raw = result.get("result") or result.get("error") or ""
    if not raw:
        raw = next((m["error"] for m in reversed(messages) if m.get("error")), "")
    msg = redact(str(raw)) or "(no error text in the execution file)"
    subtype = str(result.get("subtype", ""))
    hay = f"{msg} {subtype}".lower()
    kind, todo = "unknown", _UNKNOWN_TODO
    for name, words, what in _KINDS:
        if any(w in hay for w in words):
            kind, todo = name, what
            break
    return {
        "kind": kind,
        "message": msg,
        "subtype": subtype or "?",
        "turns": str(result.get("num_turns", "?")),
        "cost": str(result.get("total_cost_usd", "?")),
        "todo": todo,
    }


def main() -> None:
    path = Path(os.environ.get("EXEC_FILE") or "")
    if not path.is_file():
        path = Path(os.environ.get("RUNNER_TEMP", ".")) / "claude-execution-output.json"
    info = summarize(load_messages(path))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(
                f"### Auto-diagnosis could not run: {info['kind']}\n\n"
                f"- Error: `{info['message']}`\n"
                f"- Result subtype: `{info['subtype']}`, turns: {info['turns']}, cost: {info['cost']} USD\n"
                f"- What to do: {info['todo']}\n"
            )
    sys.stdout.write(
        f"The automatic diagnosis of this nightly failure could not run ({info['kind']}).\n\n"
        f"- Error: `{info['message']}`\n"
        f"- What to do: {info['todo']}\n"
        f"- Run: {os.environ.get('RUN_URL', '')}\n"
    )


if __name__ == "__main__":
    main()
