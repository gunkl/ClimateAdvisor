"""Issue #1625: the nightly auto-diagnosis says why it could not run, without exposing the transcript or a token."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "autodiag_failure.py"
_spec = importlib.util.spec_from_file_location("autodiag_failure", _SCRIPT)
autodiag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autodiag)


def _result(**fields):
    return {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "total_cost_usd": 0, **fields}


@pytest.mark.parametrize(
    ("text", "subtype", "kind"),
    [
        ("Invalid API key · Please run /login", "success", "authentication"),
        ("OAuth token has expired", "success", "authentication"),
        ("Claude AI usage limit reached|1763600000", "success", "usage limit"),
        ("API Error: 529 Overloaded", "success", "API unavailable"),
        ("", "error_max_turns", "turn limit"),
        ("something nobody anticipated", "success", "unknown"),
    ],
)
def test_each_failure_kind_is_named(text, subtype, kind):
    info = autodiag.summarize([{"type": "system"}, _result(result=text, subtype=subtype)])
    assert info["kind"] == kind


def test_the_json_array_and_the_one_message_per_line_forms_both_read(tmp_path):
    msgs = [{"type": "system", "subtype": "init"}, _result(result="Invalid API key · Please run /login")]
    array = tmp_path / "a.json"
    array.write_text(json.dumps(msgs), encoding="utf-8")
    ndjson = tmp_path / "b.json"
    ndjson.write_text("\n".join(json.dumps(m) for m in msgs), encoding="utf-8")
    assert autodiag.load_messages(array) == autodiag.load_messages(ndjson) == msgs


def test_token_like_text_never_reaches_the_summary_or_the_comment():
    leak = "boom sk-ant-oat01-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij0123456789 and " + "x" * 50
    info = autodiag.summarize([_result(result=leak)])
    assert "sk-ant" not in info["message"] and "x" * 40 not in info["message"]
    assert info["message"].count("[redacted]") == 2


def test_only_the_last_result_message_is_used_never_the_transcript():
    msgs = [
        {"type": "assistant", "message": {"content": "private transcript text"}},
        _result(result="first try"),
        _result(result="Invalid API key · Please run /login"),
    ]
    info = autodiag.summarize(msgs)
    assert info["message"] == "Invalid API key · Please run /login"
    assert "transcript" not in json.dumps(info)


def test_a_missing_execution_file_still_reports_and_comments(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("EXEC_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("RUN_URL", "https://example/run/1")
    autodiag.main()
    out = capsys.readouterr().out
    assert "could not run (unknown)" in out and "https://example/run/1" in out
    assert "(no error text in the execution file)" in summary.read_text(encoding="utf-8")


def test_the_real_20_november_failure_shape_reads_as_unknown_without_text(tmp_path, monkeypatch, capsys):
    """Run 44774348320: a result with is_error and no text at all (the action had hidden it) -- the comment still
    explains the next step instead of saying nothing."""
    exec_file = tmp_path / "claude-execution-output.json"
    exec_file.write_text(json.dumps([{"type": "system", "subtype": "init"}, _result()]), encoding="utf-8")
    monkeypatch.setenv("EXEC_FILE", str(exec_file))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    autodiag.main()
    assert "re-run the job in debug mode" in capsys.readouterr().out
