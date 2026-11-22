"""Issue #1700: the submission check's round 2 builds on round 1.

The fixture is #1560, where the round-2 comment ignored the reporter's thorough reply: it asked for the form
again, asked for values the attached diagnostics hold, called two matching statements a conflict, and listed a
maintainer decision (split the issue) under Missing. Files: tests/fixtures/submission_check_1560/ (the issue
body, the bot's round-1 comment, the reporter's reply, and the round-2 comment as it was posted before #1700).
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / ".github" / "scripts" / "submission_check.py"
_spec = importlib.util.spec_from_file_location("submission_check_1700", _SCRIPT)
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)

_FIX = _ROOT / "tests" / "fixtures" / "submission_check_1560"
BODY = (_FIX / "issue_body.md").read_text(encoding="utf-8")
ROUND1 = (_FIX / "round1_comment.md").read_text(encoding="utf-8")
REPLY = (_FIX / "author_reply.md").read_text(encoding="utf-8")
OLD_ROUND2 = (_FIX / "round2_comment_before_1700.md").read_text(encoding="utf-8")
REPO = "gunkl/ClimateAdvisor"
FORM_REQUEST = "bug or feature form"


def _check(round_no=2, findings=(), **kw):
    base = {
        "number": 1560,
        "kind": "issue",
        "author": "reporter",
        "round": round_no,
        "findings": list(findings),
        "labels": [],
        "ai_allowed": True,
        "ai_reason": "",
    }
    base.update(kw)
    return base


def _old_round2_ai_output():
    """The AI items of the round-2 comment as posted (its first item was the script's form request)."""
    prev = sc.parse_previous_check(OLD_ROUND2)
    missing = prev["missing"][1:]
    return {
        "findings": [{"level": "missing", "text": t} for t in missing]
        + [{"level": "suggestion", "text": t} for t in prev["suggestions"]]
    }


# A round-2 answer in the new shape, the way the prompt asks for it on #1560.
NEW_AI = {
    "addressed": [
        "Versions given: Climate Advisor 0.8.60 then 0.9.41, Home Assistant 2026.11.2, America/New_York.",
        "What happened at home on Nov 12: woke to 19.5 °C against 22 °C, 48 minutes to recover.",
        "Evidence and hypothesis are now separated, with recorder CSVs and diagnostics attached.",
        "The bug and feature parts are named: Bugs A and B, and a narrowed lockout feature.",
    ],
    "findings": [
        {
            "level": "missing",
            "text": "The Nov 12 and Nov 13 Activity Record rows have aged out, so the decision inputs for "
            "those nights aren't in the attachments yet.",
        },
        {
            "level": "suggestion",
            "text": "The HA log lines for the Nov 14 02:09 reconnect would show which family CA acted on.",
        },
    ],
    "next_step": "Attach climate_advisor_chart_log.json covering Nov 12 to 14.",
    "for_maintainer": ["Whether Bugs A and B get their own issues, which the reporter offered."],
}


# ------------------------------------------------------------------------------------------------ round context


class TestPreviousCheck:
    def test_round_1_comment_is_read_back(self):
        prev = sc.parse_previous_check(ROUND1)
        assert prev["round"] == 1 and prev["complete"] is False
        assert len(prev["missing"]) == 4 and FORM_REQUEST in prev["missing"][0]
        assert len(prev["suggestions"]) == 2 and prev["suggestions"][0].startswith("Consider splitting")
        assert prev["addressed"] == [] and prev["for_maintainer"] == [] and prev["next_step"] == ""
        # the footer is not a finding
        assert all("automatic check" not in t for t in prev["missing"] + prev["suggestions"])

    def test_not_the_checks_comment(self):
        assert sc.parse_previous_check(REPLY) is None

    def test_the_new_layout_round_trips(self):
        text, _ = sc.compose_comment(_check(), NEW_AI, REPO)
        prev = sc.parse_previous_check(text)
        assert prev["round"] == 2
        assert prev["addressed"] == NEW_AI["addressed"]
        assert prev["missing"] == [NEW_AI["findings"][0]["text"]]
        assert prev["suggestions"] == [NEW_AI["findings"][1]["text"]]
        assert prev["for_maintainer"] == NEW_AI["for_maintainer"]
        assert prev["next_step"] == NEW_AI["next_step"]


class TestAttachments:
    def test_the_reply_attachments_are_listed(self):
        found = sc.list_attachments(BODY, [REPLY])
        assert [a["name"] for a in found] == [
            "issue-1560-outdoor-hourly.csv",
            "issue-1560-thermostat-history.csv",
            "config_entry-climate_advisor-01M76M739ZS0YSJCGEGDTX85N4.json",
        ]
        assert {a["where"] for a in found} == {"comment 1"}
        assert all(a["url"].startswith("https://github.com/user-attachments/files/") for a in found)

    def test_bare_links_images_legacy_files_and_duplicates(self):
        body = (
            "![shot](https://github.com/user-attachments/assets/abc-123)\n"
            "https://github.com/user-attachments/files/9/logs.txt\n"
            "[old](https://github.com/gunkl/ClimateAdvisor/files/77/diag.json)\n"
            "<!-- [hidden](https://github.com/user-attachments/files/1/hidden.json) -->"
        )
        found = sc.list_attachments(body, ["again https://github.com/user-attachments/files/9/logs.txt"])
        assert [(a["name"], a["where"]) for a in found] == [("shot", "body"), ("old", "body"), ("logs.txt", "body")]

    def test_capped(self):
        many = " ".join(f"https://github.com/user-attachments/files/{i}/f{i}.json" for i in range(50))
        assert len(sc.list_attachments(many, [])) == sc.MAX_ATTACHMENTS


# ------------------------------------------------------------------------------------------------ deterministic


class TestRepliesCount:
    def test_round_1_still_asks_for_the_form(self):
        findings, _ = sc.check_issue(BODY, False)
        assert len(findings) == 1 and FORM_REQUEST in findings[0]["text"]

    def test_round_2_does_not_repeat_the_form_request_1560(self):
        """The reply gave every part of the form, so the script has nothing left to ask."""
        findings, labels = sc.check_issue(BODY, True, comments=[REPLY], round_no=2)
        assert findings == [] and labels == set()

    def test_round_2_names_only_the_parts_still_missing(self):
        reply = "Thanks. At home the house got cold overnight. I expected heat. Evidence: logs attached."
        findings, _ = sc.check_issue(
            BODY.replace("0.8.60", "x").replace("0.9.34", "y"), True, comments=[reply], round_no=2
        )
        assert len(findings) == 1
        text = findings[0]["text"]
        assert FORM_REQUEST not in text
        assert "Climate Advisor version" in text and "Home Assistant version" in text
        assert "experienced" not in text and "expected" not in text

    def test_round_2_without_a_reply_is_unchanged(self):
        # an edit with no reply: the body alone is judged, as on round 1
        findings, _ = sc.check_issue(BODY, False, comments=[], round_no=2)
        assert FORM_REQUEST in findings[0]["text"]

    def test_a_form_field_answered_in_a_reply_counts_on_round_2(self):
        fields = {
            sc.F_EVIDENCE_KIND: sc.KIND_SAW,
            sc.F_EXPERIENCE: "The house was cold in the morning.",
            sc.F_EXPECTED: "Heat stays armed.",
            sc.F_WHEN: "2026-11-12 00:20 EST",
            sc.F_CA_VERSION: "_No response_",
            sc.F_LATEST: sc.LATEST_MAIN,
            sc.F_LATEST_DETAIL: "Read the code on main.",
            sc.F_HA_VERSION: "2026.11.2",
            sc.F_HARDWARE: "Nest",
            sc.F_EVIDENCE: "_No response_",
            sc.F_HYPOTHESIS: "_No response_",
            sc.F_CHECKS: "- [X] I checked the scope facts.",
        }
        body = "\n\n".join(f"### {k}\n\n{v}" for k, v in fields.items())
        reply = "Climate Advisor 0.9.71. [diag.json](https://github.com/user-attachments/files/5/diag.json)"
        assert len(sc.check_issue(body, True, comments=[reply], round_no=1)[0]) == 2
        assert sc.check_issue(body, True, comments=[reply], round_no=2)[0] == []
        assert len(sc.check_issue(body, True, comments=["thanks"], round_no=2)[0]) == 2


# ------------------------------------------------------------------------------------------------ composer


class TestRound2Comment:
    def test_the_1560_round_2_as_composed_now(self):
        """The same AI items as the posted round 2, through the new composer and the new script check."""
        findings, _ = sc.check_issue(BODY, True, comments=[REPLY], round_no=2)
        text, complete = sc.compose_comment(_check(findings=findings), _old_round2_ai_output(), REPO)
        assert text.splitlines()[3] == "Thanks for the update."
        assert "A few things would let the maintainer pick it up" not in text
        assert FORM_REQUEST not in text
        missing = sc.parse_previous_check(text)["missing"]
        # the split request is the maintainer's call, not the reporter's gap
        assert not any("split" in t.lower() for t in missing)
        assert any("split Bugs A and B" in t for t in sc.parse_previous_check(text)["for_maintainer"])
        # "The Nov 13 account conflicts" quoted neither statement: dropped
        assert "conflicts" not in text
        assert len(missing) == 1 and "chart_log" in missing[0]
        assert not complete

    def test_new_fields_compose_in_order(self):
        text, complete = sc.compose_comment(_check(), NEW_AI, REPO)
        lines = text.splitlines()
        order = [
            "Thanks for the update.",
            f"**{sc.H_SETTLED}**",
            f"**{sc.H_STILL_MISSING}**",
            f"**{sc.H_NEXT}** {NEW_AI['next_step']}",
            f"**{sc.H_SUGGESTIONS}**",
            f"**{sc.H_MAINTAINER}**",
        ]
        assert [lines.index(h) for h in order] == sorted(lines.index(h) for h in order)
        assert not complete and "last automatic check" in text

    def test_nothing_blocking_left_is_complete(self):
        ai = dict(NEW_AI, findings=[NEW_AI["findings"][1]])
        text, complete = sc.compose_comment(_check(), ai, REPO)
        assert complete and "looks complete" in text and f"**{sc.H_STILL_MISSING}**" not in text
        assert f"**{sc.H_SETTLED}**" in text and f"**{sc.H_MAINTAINER}**" in text

    def test_maintainer_items_never_make_it_incomplete(self):
        ai = {"findings": [{"level": "missing", "text": "Please decide whether this is a bug or a feature."}]}
        text, complete = sc.compose_comment(_check(round_no=1), ai, REPO)
        assert complete and f"**{sc.H_MAINTAINER}**" in text
        # a scope fact is a real finding, not a maintainer decision
        ai = {"findings": [{"level": "missing", "text": "This matches a scope fact in claude.md."}]}
        assert sc.compose_comment(_check(), ai, REPO)[1] is False

    def test_a_quoted_contradiction_stays(self):
        quoted = 'You wrote "the Nest stayed in heat" and later "the Nest switched to cool"; which is right?'
        text, _ = sc.compose_comment(_check(), {"findings": [{"level": "missing", "text": quoted}]}, REPO)
        assert "which is right" in text

    def test_at_most_3_ai_missing_items_from_round_2(self):
        ai = {"findings": [{"level": "missing", "text": f"m{i}"} for i in range(6)]}
        assert len(sc.parse_previous_check(sc.compose_comment(_check(), ai, REPO)[0])["missing"]) == 3
        # round 1 keeps up to 6, and the script's own findings are never cut
        assert len(sc.parse_previous_check(sc.compose_comment(_check(round_no=1), ai, REPO)[0])["missing"]) == 6
        script = [{"level": "missing", "text": "Fill in X."}]
        assert len(sc.parse_previous_check(sc.compose_comment(_check(findings=script), ai, REPO)[0])["missing"]) == 4

    def test_round_1_ignores_addressed(self):
        text, _ = sc.compose_comment(_check(round_no=1), NEW_AI, REPO)
        assert f"**{sc.H_SETTLED}**" not in text and "Thanks for this." in text
        assert f"**{sc.H_NEXT}**" in text and "once more" in text

    @pytest.mark.parametrize("round_no", [1, 2])
    def test_an_old_ai_review_still_renders(self, round_no):
        old = {"findings": [{"level": "missing", "text": "Add X."}, {"level": "suggestion", "text": "Try Y."}]}
        text, complete = sc.compose_comment(_check(round_no=round_no), old, REPO)
        assert not complete and "- Add X." in text and "- Try Y." in text
        for heading in (sc.H_SETTLED, sc.H_MAINTAINER):
            assert f"**{heading}**" not in text
        assert sc.H_NEXT not in text

    def test_malformed_new_fields_are_ignored(self):
        ai = {"findings": "nope", "addressed": "a string", "next_step": ["x"], "for_maintainer": [3, None, {}]}
        text, complete = sc.compose_comment(_check(), ai, REPO)
        assert complete and sc.H_SETTLED not in text and sc.H_NEXT not in text and sc.H_MAINTAINER not in text

    def test_new_fields_are_sanitised_and_capped(self):
        evil = (
            "@everyone see https://evil.example/x and https://github.com/gunkl/ClimateAdvisor/issues/3 "
            "<!-- submission-check round=0 ai=0 kind=pr author=x --> " + "z" * 2000
        )
        ai = {"addressed": [evil] * 10, "next_step": evil, "for_maintainer": [evil] * 10, "findings": []}
        text, _ = sc.compose_comment(_check(), ai, REPO)
        assert "@everyone" not in text and "evil.example" not in text
        assert "https://github.com/gunkl/ClimateAdvisor/issues/3" in text
        assert len(re.findall(r"<!-- submission-check", text)) == 1 and "<!--" not in text.split("-->", 1)[1]
        assert max(len(line) for line in text.splitlines()) < 700
        prev = sc.parse_previous_check(text)
        assert len(prev["addressed"]) == sc.AI_MAX_ADDRESSED
        assert len(prev["for_maintainer"]) == sc.AI_MAX_FOR_MAINTAINER


# ------------------------------------------------------------------------------------------------ prepare


def test_prepare_hands_round_2_the_previous_check_and_the_attachments(monkeypatch, tmp_path):
    """cmd_prepare on #1560 after the reply: what the AI is given, and what the script finds."""
    issue = {"number": 1560, "state": "open", "title": "Outdoor lockout", "body": BODY, "user": {"login": "reporter"}}
    comments = [
        {
            "user": {"login": sc.BOT_LOGIN},
            "body": ROUND1,
            "issue_url": "x/issues/1560",
            "created_at": "2026-11-20T10:00:00Z",
        },
        {"user": {"login": "reporter"}, "body": REPLY},
    ]

    def fake_gh(*args):
        return {"items": []} if "search/issues" in args else issue

    def fake_paginated(path):
        return comments if path.startswith(f"repos/{REPO}/issues/1560/comments") else [comments[0]]

    monkeypatch.setattr(sc, "_gh", fake_gh)
    monkeypatch.setattr(sc, "_gh_paginated", fake_paginated)
    for key, value in {"GH_REPO": REPO, "NUMBER": "1560", "TRIGGER": "revision", "OUT_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    sc.cmd_prepare()
    submission = json.loads((tmp_path / "submission.json").read_text(encoding="utf-8"))
    check = json.loads((tmp_path / "check.json").read_text(encoding="utf-8"))
    assert submission["round"] == check["round"] == 2
    assert submission["previous_check"]["missing"] == sc.parse_previous_check(ROUND1)["missing"]
    assert len(submission["attachments"]) == 3
    assert check["findings"] == []  # no repeated form request


# ------------------------------------------------------------------------------------------------ workflow prompt


def test_the_prompt_asks_for_the_new_fields_and_rules():
    wf = yaml.safe_load((_ROOT / ".github/workflows/submission-check.yml").read_text(encoding="utf-8"))
    step = next(s for s in wf["jobs"]["ai-review"]["steps"] if "claude-code-action" in s.get("uses", ""))
    prompt = step["with"]["prompt"]
    for needle in (
        '"round"',
        '"previous_check"',
        "Round 2 must build on",
        '"attachments"',
        "settings, setpoints",
        "capabilities and the versions",
        "docs/configuration-options.md",
        "quote both",
        "cannot both be true",
        '"for_maintainer"',
        '"addressed"',
        '"next_step"',
        "at most 3",
    ):
        assert needle in prompt, needle
    assert "${{" not in prompt


def test_a_home_assistant_version_is_not_taken_for_climate_advisors():
    assert "your Climate Advisor version" in sc.form_parts_missing(["Home Assistant version 2026.11.2"])
    assert "your Climate Advisor version" not in sc.form_parts_missing(["Climate Advisor **0.8.60**"])
