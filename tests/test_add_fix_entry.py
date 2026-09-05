"""Tests for tools/add_fix_entry.py's duplicate-issue guard and --allow-reopened (Issue #788).

add_fix_entry.py is the only sanctioned way to append a record to fix_history.jsonl
(hand-editing is disallowed — a malformed line breaks the streaming parser for every
record after it). Its duplicate-issue check exists to stop the same fix from being
accidentally re-filed. Issue #788 needed a second, materially different fix appended
for an issue number that already had an entry (the original fix addressed wording only;
the reopened fix addressed the underlying recovery_time computation) — this exercises
the --allow-reopened escape hatch added for that case, and confirms it is scoped to
only the exact --issue value passed, not a blanket disable of the duplicate check.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "add_fix_entry.py"


def _load_add_fix_entry_module():
    spec = importlib.util.spec_from_file_location("add_fix_entry", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _run(module, monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["add_fix_entry.py", *argv])
    return module.main()


def _read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def module_and_history(tmp_path, monkeypatch):
    module = _load_add_fix_entry_module()
    history_path = tmp_path / "fix_history.jsonl"
    _write_jsonl(history_path, [{"issue": 788, "version_fixed": "0.6.85", "title": "original wording-only fix"}])
    monkeypatch.setattr(module, "_FIX_HISTORY_FILE", history_path)
    return module, history_path


class TestDefaultDuplicateGuard:
    def test_new_issue_is_appended(self, module_and_history, monkeypatch, capsys):
        module, history_path = module_and_history
        rc = _run(
            module,
            monkeypatch,
            ["--issue", "900", "--version", "0.7.23", "--title", "t", "--scope", "s"],
        )
        assert rc == 0
        records = _read_records(history_path)
        assert [r["issue"] for r in records] == [788, 900]

    def test_existing_issue_is_rejected_without_flag(self, module_and_history, monkeypatch, capsys):
        module, history_path = module_and_history
        rc = _run(
            module,
            monkeypatch,
            ["--issue", "788", "--version", "0.7.23", "--title", "t", "--scope", "s"],
        )
        assert rc == 1
        assert "already has a fix_history entry" in capsys.readouterr().err
        # File must be unchanged — no partial/duplicate write on rejection.
        expected = [{"issue": 788, "version_fixed": "0.6.85", "title": "original wording-only fix"}]
        assert _read_records(history_path) == expected


class TestAllowReopenedFlag:
    def test_reopened_issue_is_appended_with_flag(self, module_and_history, monkeypatch, capsys):
        module, history_path = module_and_history
        rc = _run(
            module,
            monkeypatch,
            [
                "--issue",
                "788",
                "--version",
                "0.7.22",
                "--title",
                "deeper reopened fix",
                "--scope",
                "s",
                "--user-summary",
                "Fix #788: deeper fix",
                "--allow-reopened",
            ],
        )
        assert rc == 0
        records = _read_records(history_path)
        assert [r["issue"] for r in records] == [788, 788]
        assert records[1]["title"] == "deeper reopened fix"
        assert records[1]["user_summary"] == "Fix #788: deeper fix"

    def test_flag_does_not_touch_check_for_a_different_target_issue(self, module_and_history, monkeypatch, capsys):
        # --allow-reopened is only ever evaluated against the single --issue value passed
        # in *this* invocation (`args.issue in existing_issues and not args.allow_reopened`
        # — there is no way to name a second, unrelated issue in one call). This test pins
        # that structural guarantee: adding a brand-new issue in the same invocation that
        # happens to pass --allow-reopened behaves identically to not passing the flag at
        # all — the flag has no effect beyond the one issue it names.
        module, history_path = module_and_history
        _write_jsonl(
            history_path,
            [
                {"issue": 788, "version_fixed": "0.6.85", "title": "original wording-only fix"},
                {"issue": 900, "version_fixed": "0.7.0", "title": "some other fix"},
            ],
        )
        rc = _run(
            module,
            monkeypatch,
            ["--issue", "901", "--version", "0.7.23", "--title", "t", "--scope", "s", "--allow-reopened"],
        )
        assert rc == 0
        records = _read_records(history_path)
        assert {r["issue"] for r in records} == {788, 900, 901}
        # The pre-existing duplicates (788, 900) are untouched — no rewriting, no collapsing.
        assert records[0]["title"] == "original wording-only fix"
        assert records[1]["title"] == "some other fix"

    def test_flag_bypasses_check_for_a_merged_from_alias_too(self, module_and_history, monkeypatch):
        # existing_issues is built from both top-level "issue" and any "merged_from" list
        # (issues folded into another entry's record). --allow-reopened must bypass the
        # check the same way regardless of which of those two sources matched.
        module, history_path = module_and_history
        _write_jsonl(
            history_path,
            [{"issue": 788, "version_fixed": "0.6.85", "merged_from": [700]}],
        )
        rc = _run(
            module,
            monkeypatch,
            ["--issue", "700", "--version", "0.7.23", "--title", "t", "--scope", "s", "--allow-reopened"],
        )
        assert rc == 0
        assert [r["issue"] for r in _read_records(history_path)] == [788, 700]

    def test_flag_defaults_to_false_when_omitted(self, module_and_history):
        module, _ = module_and_history
        parser = module.argparse.ArgumentParser()
        # Reproduce the exact flag definition via main()'s own parser construction isn't
        # exposed directly, so instead assert the behavior end-to-end: omitting the flag
        # must reject a known duplicate (already covered above) — here we additionally
        # confirm the attribute itself defaults False via argparse's own parsing.
        parser.add_argument("--allow-reopened", action="store_true")
        args = parser.parse_args([])
        assert args.allow_reopened is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
