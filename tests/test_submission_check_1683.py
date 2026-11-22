"""Issue #1683: the automatic submission check (.github/scripts/submission_check.py)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / ".github" / "scripts" / "submission_check.py"
_spec = importlib.util.spec_from_file_location("submission_check", _SCRIPT)
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)

OWNER = "owner"
NOW = dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.UTC)


def _issue_event(action="opened", author="alice", sender=None, labels=(), changes=None, pr=False, state="open"):
    issue = {"number": 7, "state": state, "user": {"login": author}, "labels": [{"name": n} for n in labels]}
    if pr:
        issue["pull_request"] = {"url": "x"}
    return {"action": action, "issue": issue, "sender": {"login": sender or author}, "changes": changes or {}}


def _pr_event(action="opened", author="alice", sender=None):
    pr = {"number": 9, "state": "open", "user": {"login": author}, "labels": []}
    return {"action": action, "pull_request": pr, "sender": {"login": sender or author}, "changes": {"body": {}}}


def _comment_event(commenter, body="thanks", author="alice", pr=False):
    ev = _issue_event(action="created", author=author, pr=pr)
    ev["comment"] = {"user": {"login": commenter}, "body": body}
    return ev


# ------------------------------------------------------------------------------------------------ gate


class TestGate:
    def test_new_issue_and_pr_are_checked(self):
        assert sc.classify_event("issues", _issue_event(), OWNER) == {
            "number": 7,
            "kind": "issue",
            "author": "alice",
            "trigger": "opened",
        }
        assert sc.classify_event("pull_request_target", _pr_event(), OWNER)["kind"] == "pr"

    @pytest.mark.parametrize("author", [OWNER, "dependabot[bot]", "github-actions[bot]"])
    def test_skipped_authors(self, author):
        assert sc.classify_event("issues", _issue_event(author=author), OWNER) is None
        assert sc.classify_event("pull_request_target", _pr_event(author=author), OWNER) is None

    def test_nightly_failure_issues_and_closed_items_are_skipped(self):
        assert sc.classify_event("issues", _issue_event(labels=["nightly-failure"]), OWNER) is None
        assert sc.classify_event("issues", _issue_event(state="closed"), OWNER) is None

    def test_the_autofix_bot_pr_is_checked(self):
        assert sc.classify_event("pull_request_target", _pr_event(author="claude[bot]"), OWNER) is not None

    def test_revisions_only_by_the_author(self):
        body_edit = {"body": {"from": "x"}}
        assert sc.classify_event("issues", _issue_event("edited", changes=body_edit), OWNER)["trigger"] == "revision"
        assert sc.classify_event("issues", _issue_event("edited", sender="bob", changes=body_edit), OWNER) is None
        # a label or milestone edit is not a revision
        assert sc.classify_event("issues", _issue_event("edited", changes={"milestone": {}}), OWNER) is None
        assert sc.classify_event("pull_request_target", _pr_event("synchronize"), OWNER)["trigger"] == "revision"
        assert sc.classify_event("pull_request_target", _pr_event("synchronize", sender="bob"), OWNER) is None

    def test_comments(self):
        assert sc.classify_event("issue_comment", _comment_event("alice"), OWNER)["trigger"] == "revision"
        assert sc.classify_event("issue_comment", _comment_event("bob"), OWNER) is None
        assert (
            sc.classify_event("issue_comment", _comment_event(OWNER, "/recheck please"), OWNER)["trigger"] == "recheck"
        )
        assert sc.classify_event("issue_comment", _comment_event(OWNER, "looks good"), OWNER) is None
        # only the owner can ask for a recheck
        assert sc.classify_event("issue_comment", _comment_event("bob", "/recheck"), OWNER) is None
        assert sc.classify_event("issue_comment", _comment_event("alice", pr=True), OWNER)["kind"] == "pr"


# ------------------------------------------------------------------------------------------------ rounds and limits


class TestRounds:
    def test_at_most_two_rounds(self):
        assert sc.decide_round(0, "opened") == 1
        assert sc.decide_round(0, "revision") == 1  # round 1 was cancelled by a newer change
        assert sc.decide_round(1, "revision") == 2
        assert sc.decide_round(1, "opened") is None
        assert sc.decide_round(2, "revision") is None
        assert sc.decide_round(5, "revision") is None

    def test_recheck_always_runs(self):
        assert sc.decide_round(2, "recheck") == 3

    def test_marker_round_trip(self):
        marker = sc.make_marker(2, True, "pr", "alice")
        assert sc.parse_marker("text\n" + marker + "\nmore") == {
            "round": "2",
            "ai": "1",
            "kind": "pr",
            "author": "alice",
        }
        assert sc.parse_marker("no marker") is None


def _entry(number, author="x", kind="issue", hours_ago=1.0):
    return {"number": number, "author": author, "kind": kind, "at": NOW - dt.timedelta(hours=hours_ago)}


class TestRateLimits:
    def _allowed(self, ledger, number=99, kind="issue", author="alice", recheck=False):
        return sc.ai_allowed(ledger, now=NOW, number=number, kind=kind, author=author, recheck=recheck)

    def test_global_daily_limit_counts_distinct_submissions(self):
        ledger = [_entry(n, author=f"u{n}") for n in range(9)] + [_entry(0, author="u0")]  # 9 distinct
        assert self._allowed(ledger)[0] is True
        ledger.append(_entry(9, author="u9"))
        assert self._allowed(ledger) == (False, "the project's daily limit")
        # older than 24 h no longer counts
        assert self._allowed([_entry(n, author=f"u{n}", hours_ago=25) for n in range(10)])[0] is True

    def test_per_author_daily_limit_is_per_kind(self):
        ledger = [_entry(n, author="alice", kind="issue") for n in range(3)]
        assert self._allowed(ledger) == (False, "your daily limit")
        assert self._allowed(ledger, kind="pr")[0] is True
        assert self._allowed(ledger, author="bob")[0] is True

    def test_per_author_monthly_limit(self):
        ledger = [_entry(n, author="alice", hours_ago=48 + n) for n in range(30)]
        assert self._allowed(ledger) == (False, "your monthly limit")
        ledger = [_entry(n, author="alice", hours_ago=31 * 24 + n) for n in range(30)]
        assert self._allowed(ledger)[0] is True

    def test_second_round_and_recheck_never_count_again(self):
        full = [_entry(n, author=f"u{n}") for n in range(10)]
        assert self._allowed(full, number=3)[0] is True  # already counted
        assert self._allowed(full, recheck=True)[0] is True


# ------------------------------------------------------------------------------------------------ issue checks


def _bug(**overrides):
    fields = {
        sc.F_EVIDENCE_KIND: sc.KIND_SAW,
        sc.F_EXPERIENCE: "The AC ran with the windows open.",
        sc.F_EXPECTED: "The AC stays off.",
        sc.F_WHEN: "2026-11-20 14:00 PST",
        sc.F_CA_VERSION: "0.9.67",
        sc.F_HA_VERSION: "2026.11.1",
        sc.F_HARDWARE: "Ecobee, fan mode hvac_fan",
        sc.F_EVIDENCE: "[diag.json](https://github.com/user-attachments/files/1/diag.json)",
        sc.F_HYPOTHESIS: "_No response_",
        sc.F_CHECKS: "- [X] I checked the scope facts (claude.md) and the closed wontfix issues for this behaviour.",
    }
    fields.update(overrides)
    return "\n\n".join(f"### {k}\n\n{v}" for k, v in fields.items())


def _levels(findings):
    return [f["level"] for f in findings]


class TestIssueChecks:
    def test_complete_bug_report(self):
        findings, labels = sc.check_issue(_bug(), author_replied=False)
        assert findings == [] and labels == set()

    def test_evidence_needs_data_and_a_time(self):
        findings, _ = sc.check_issue(_bug(**{sc.F_EVIDENCE: "_No response_", sc.F_WHEN: "_No response_"}), False)
        assert _levels(findings) == ["missing", "missing"]
        findings, _ = sc.check_issue(_bug(**{sc.F_EVIDENCE: "it happened, trust me"}), False)
        assert any("Attach" in f["text"] for f in findings)

    def test_hypothesis_is_labelled_and_needs_what_would_confirm_it(self):
        body = _bug(
            **{sc.F_EVIDENCE_KIND: sc.KIND_HYPOTHESIS, sc.F_EVIDENCE: "_No response_", sc.F_WHEN: "_No response_"}
        )
        findings, labels = sc.check_issue(body, False)
        assert labels == {sc.LABEL_HYPOTHESIS}
        assert len(findings) == 1 and "confirm" in findings[0]["text"]
        findings, _ = sc.check_issue(
            body.replace(
                "### " + sc.F_HYPOTHESIS + "\n\n_No response_",
                "### " + sc.F_HYPOTHESIS + "\n\nautomation.py, _x(); a log line",
            ),
            False,
        )
        assert findings == []

    def test_harness_reproduction_needs_the_command(self):
        body = _bug(**{sc.F_EVIDENCE_KIND: sc.KIND_HARNESS, sc.F_EVIDENCE: "_No response_"})
        assert _levels(sc.check_issue(body, False)[0]) == ["missing"]

    def test_required_fields_and_unticked_check(self):
        findings, _ = sc.check_issue(_bug(**{sc.F_CA_VERSION: "_No response_", sc.F_CHECKS: "- [ ] I checked"}), False)
        assert _levels(findings) == ["missing", "missing"]

    def test_missing_hardware_is_only_a_suggestion(self):
        assert _levels(sc.check_issue(_bug(**{sc.F_HARDWARE: "_No response_"}), False)[0]) == ["suggestion"]

    def test_feature_form(self):
        assert sc.check_issue(f"### {sc.F_OUTCOME}\n\nWarmer mornings", False)[0] == []
        assert _levels(sc.check_issue(f"### {sc.F_OUTCOME}\n\n_No response_", False)[0]) == ["missing"]

    def test_free_text_issue_is_pointed_at_the_forms(self):
        findings, _ = sc.check_issue("The fan is broken", False)
        assert _levels(findings) == ["missing"] and "form" in findings[0]["text"]

    def test_dashboard_report_only_needs_the_occupant_story(self):
        body = "## Activity Record\n\n| a |\n\n---\n*Generated by Climate Advisor*"
        assert _levels(sc.check_issue(body, author_replied=False)[0]) == ["missing"]
        assert sc.check_issue(body, author_replied=True)[0] == []


# ------------------------------------------------------------------------------------------------ PR checks


def _pr_body(fill="Something real.", ticked=True):
    box = "[x]" if ticked else "[ ]"
    parts = ["Fixes #12"]
    for heading in sc.PR_SECTIONS:
        content = f"- {box} unit tests" if heading == "Verification" else fill
        parts.append(f"## {heading}\n<!-- hint -->\n{content}")
    return "\n\n".join(parts)


CODE_FILES = [
    "custom_components/climate_advisor/automation.py",
    "custom_components/climate_advisor/const.py",
    "custom_components/climate_advisor/manifest.json",
    "custom_components/climate_advisor/fix_history.jsonl",
    "CHANGELOG.md",
]
BUG = {"number": 12, "labels": [{"name": "bug"}]}


class TestPrChecks:
    def test_complete_pr(self):
        assert sc.check_pr(_pr_body(), CODE_FILES, BUG, 12) == []

    def test_linked_issue_is_required(self):
        body = _pr_body().replace("Fixes #12", "")
        assert sc.linked_issue(body) is None
        assert "Link the issue" in sc.check_pr(body, CODE_FILES, None, None)[0]["text"]
        assert sc.linked_issue("Closes: #5") == 5 and sc.linked_issue("refs #6") == 6
        assert sc.linked_issue("<!-- Fixes #1 -->") is None  # the template's own hint is not a link

    def test_a_pr_number_is_not_an_issue(self):
        findings = sc.check_pr(_pr_body(), CODE_FILES, {"number": 12, "pull_request": {}}, 12)
        assert "not an issue" in findings[0]["text"]

    def test_a_feature_needs_the_approach_agreed(self):
        feature = {"number": 12, "labels": [{"name": "enhancement"}]}
        assert "approach agreed" in sc.check_pr(_pr_body(), CODE_FILES, feature, 12)[0]["text"]
        feature["labels"].append({"name": sc.LABEL_APPROACH_AGREED})
        assert sc.check_pr(_pr_body(), CODE_FILES, feature, 12) == []

    def test_empty_or_missing_sections(self):
        findings = sc.check_pr(_pr_body(fill=""), CODE_FILES, BUG, 12)
        assert sum(1 for f in findings if f["text"].startswith("Fill in")) == len(sc.PR_SECTIONS) - 1
        body = _pr_body().replace("## Mirror check", "## Something else")
        assert any('Add the "Mirror check"' in f["text"] for f in sc.check_pr(body, CODE_FILES, BUG, 12))

    def test_unticked_verification_is_a_suggestion(self):
        assert _levels(sc.check_pr(_pr_body(ticked=False), CODE_FILES, BUG, 12)) == ["suggestion"]

    def test_housekeeping_files(self):
        findings = sc.check_pr(_pr_body(), CODE_FILES[:1], BUG, 12)
        assert len(findings) == 1 and "const.py" in findings[0]["text"] and "fix_history" in findings[0]["text"]

    def test_small_changes_need_no_issue_and_only_the_first_section(self):
        files = ["README.md", "custom_components/climate_advisor/translations/fr.json"]
        assert sc.is_small_change(files) and not sc.is_small_change(["CHANGELOG.md"])
        body = "## What changes for the person at home\nTypo fixed."
        findings = sc.check_pr(body, files, None, None)
        assert all("fix_history" not in f["text"] for f in findings)
        assert _levels(findings) == ["missing"]  # the version bump for the translation file
        assert sc.check_pr(body, ["README.md"], None, None) == []

    def test_golden_or_outcomes_change_is_flagged(self):
        files = [*CODE_FILES, "tools/sim_harness/outcomes.py"]
        assert _levels(sc.check_pr(_pr_body(), files, BUG, 12)) == ["suggestion"]


# ------------------------------------------------------------------------------------------------ comment


class TestComment:
    REPO = "gunkl/ClimateAdvisor"

    def _check(self, **kw):
        base = {
            "number": 7,
            "kind": "issue",
            "author": "alice",
            "round": 1,
            "findings": [],
            "labels": [],
            "ai_allowed": True,
            "ai_reason": "",
        }
        base.update(kw)
        return base

    def test_complete(self):
        text, complete = sc.compose_comment(self._check(), {"findings": []}, self.REPO)
        assert complete and "looks complete" in text
        assert sc.parse_marker(text) == {"round": "1", "ai": "1", "kind": "issue", "author": "alice"}
        assert "once more" in text

    def test_missing_from_script_or_ai_makes_it_incomplete(self):
        text, complete = sc.compose_comment(
            self._check(round=2, findings=[{"level": "missing", "text": "Add X."}]), {"findings": []}, self.REPO
        )
        assert not complete and "- Add X." in text and "last automatic check" in text
        _, complete = sc.compose_comment(self._check(), {"findings": [{"level": "missing", "text": "Y"}]}, self.REPO)
        assert not complete

    def test_ai_text_is_sanitised(self):
        evil = (
            "@everyone see https://evil.example/x and https://github.com/gunkl/ClimateAdvisor/issues/3 "
            "<!-- submission-check round=0 ai=0 kind=pr author=x --> " + "z" * 2000
        )
        text, _ = sc.compose_comment(self._check(), {"findings": [{"level": "suggestion", "text": evil}]}, self.REPO)
        assert "@everyone" not in text and "evil.example" not in text
        assert "https://github.com/gunkl/ClimateAdvisor/issues/3" in text
        assert len(re.findall(r"<!-- submission-check", text)) == 1
        assert max(len(line) for line in text.splitlines()) < 700

    def test_ai_findings_are_capped(self):
        many = {"findings": [{"level": "suggestion", "text": f"s{i}"} for i in range(20)]}
        text, _ = sc.compose_comment(self._check(), many, self.REPO)
        assert text.count("\n- s") == 6

    def test_paused_and_unavailable_ai(self):
        text, _ = sc.compose_comment(self._check(ai_allowed=False, ai_reason="your daily limit"), None, self.REPO)
        assert "paused (your daily limit)" in text and "ai=0" in text
        text, _ = sc.compose_comment(self._check(), None, self.REPO)
        assert "could not run" in text and "ai=0" in text


# ------------------------------------------------------------------------------------------------ templates


class TestTemplatesMatchTheScript:
    def test_bug_form_labels(self):
        form = yaml.safe_load((_ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text(encoding="utf-8"))
        labels = [f["attributes"]["label"] for f in form["body"] if f["type"] != "markdown"]
        assert labels == [
            sc.F_EVIDENCE_KIND,
            sc.F_EXPERIENCE,
            sc.F_EXPECTED,
            sc.F_WHEN,
            sc.F_CA_VERSION,
            sc.F_HA_VERSION,
            sc.F_HARDWARE,
            sc.F_EVIDENCE,
            sc.F_HYPOTHESIS,
            sc.F_CHECKS,
        ]
        kinds = next(f for f in form["body"] if f.get("id") == "evidence_kind")["attributes"]["options"]
        assert kinds == [sc.KIND_SAW, sc.KIND_HARNESS, sc.KIND_HYPOTHESIS]

    def test_feature_form_and_config(self):
        form = yaml.safe_load((_ROOT / ".github/ISSUE_TEMPLATE/feature_request.yml").read_text(encoding="utf-8"))
        assert form["body"][1]["attributes"]["label"] == sc.F_OUTCOME
        config = yaml.safe_load((_ROOT / ".github/ISSUE_TEMPLATE/config.yml").read_text(encoding="utf-8"))
        assert config["blank_issues_enabled"] is False

    def test_pr_template_sections(self):
        text = (_ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8")
        assert tuple(re.findall(r"^## (.+)$", text, re.MULTILINE)) == sc.PR_SECTIONS
        # the empty template is incomplete: every section but the two checkbox lists is a placeholder
        findings = sc.check_pr(text, CODE_FILES, BUG, 12)
        assert any("Link the issue" in f["text"] or "Fill in" in f["text"] for f in findings)

    def test_the_workflow_keeps_the_ai_job_read_only(self):
        wf = yaml.safe_load((_ROOT / ".github/workflows/submission-check.yml").read_text(encoding="utf-8"))
        jobs = wf["jobs"]
        assert jobs["ai-review"]["permissions"] == {"contents": "read", "id-token": "write"}
        assert "id-token" not in jobs["post"]["permissions"]
        assert all("ref" not in (s.get("with") or {}) for j in jobs.values() for s in j["steps"])  # never PR code
        step = next(s for s in jobs["ai-review"]["steps"] if "claude-code-action" in s.get("uses", ""))
        assert '--allowedTools "Read,Glob,Grep,Write"' in step["with"]["claude_args"]
        assert "${{" not in step["with"]["prompt"]  # no event text interpolated into the prompt


def test_artifact_paths_are_not_hidden_1685():
    """Issue #1685: upload-artifact skips hidden files by default, so a dot-folder uploaded nothing."""
    wf = yaml.safe_load((_ROOT / ".github/workflows/submission-check.yml").read_text(encoding="utf-8"))
    uploads = []
    for job in wf["jobs"].values():
        for step in job["steps"]:
            uses = step.get("uses", "")
            if "upload-artifact" in uses or "download-artifact" in uses:
                path = step["with"]["path"]
                assert not any(part.startswith(".") for part in path.split("/") if part), path
                if "upload-artifact" in uses:
                    uploads.append(step["with"])
    # the inputs every later job needs must fail loudly when missing, not warn
    submission = next(u for u in uploads if u["name"] == "submission")
    assert submission["if-no-files-found"] == "error"
    # the AI writes exactly where its upload looks
    ai = next(u for u in uploads if u["name"] == "ai-review")
    review = next(s for s in wf["jobs"]["ai-review"]["steps"] if "claude-code-action" in s.get("uses", ""))
    assert f"Write ONLY the file {ai['path']}" in review["with"]["prompt"]
    env = next(s for s in wf["jobs"]["post"]["steps"] if "env" in s)["env"]
    assert env["AI_FILE"] == ai["path"]
