"""nightly_ledger - the short-lived, strict waiver ledger for the nightly generated days (Issue #1153).

Nightly seeds are fresh each run, so a known hit cannot be recognised by seed; it is recognised by
the MECHANISM that produced it (``mechanism.py``).  The ledger tolerates a hit only while ALL of this
holds, and anything else turns the gate red:

* every entry is tied to an issue number, and that issue must be OPEN (a closed issue means the fix
  landed or the work was dropped - either way the waiver is stale);
* every entry has an ``added`` and an ``expires`` date, ``expires`` is at most ``MAX_LIFETIME_DAYS``
  after ``added`` and must not have passed;
* an entry matches a hit on ``promise`` plus a non-empty subset of the mechanism fields
  (``mechanism.MATCHABLE_FIELDS``); a seed, a day or a commit can never be matched on;
* an entry that matches NO hit in the run is stale (the bug is fixed, or the run did not reach it);
* two entries with the same promise and match are a duplicate, and one hit matched by two entries is
  ambiguous - both are rejected, so one mechanism has exactly one waiver.

Same ratchet shape as ``divergence.load_ledger`` / ``check_ledger`` (a loader that rejects a malformed
row loudly, and a checker that returns problem strings for an unlisted hit or a stale entry).  That
pair is keyed by golden scenario and carries real-HA failure classes, so it cannot be reused as is.

Issue open/closed state is a separate, injectable lookup (``GitHubIssueStates``); offline runs use
``OfflineIssueStates`` which skips the closed-issue check LOUDLY (a warning, never a silent pass).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from tools.sim_harness.mechanism import MATCHABLE_FIELDS

LEDGER_PATH = Path(__file__).resolve().with_name("nightly_waivers.json")
MAX_LIFETIME_DAYS = 45  # "short-lived": a waiver is a loan against a fix, not a standing exemption
_REQUIRED = ("id", "issue", "promise", "match", "reason", "added", "expires")


# ───────────────────────────────────────────────────────────────────────────────── loading
def _parse_date(entry_id: str, key: str, value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as err:
        raise ValueError(f"nightly waiver {entry_id!r}: {key} {value!r} is not a YYYY-MM-DD date") from err


def _fingerprint(entry: Mapping[str, Any]) -> str:
    return json.dumps({"promise": entry["promise"], "match": entry["match"]}, sort_keys=True)


def load_ledger(path: Path = LEDGER_PATH, *, promise_keys: Iterable[str] | None = None) -> list[dict]:
    """The ledger rows (dates parsed to ``added_on`` / ``expires_on``); rejects any malformed row loudly.

    A silently dropped row would turn a known hit into a surprise failure, and a loose one (empty
    match, unknown field, no expiry, an uncapped lifetime) would let hits ride along for good.
    ``promise_keys`` (the production registry keys) makes an unknown promise name a load error."""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rows = data.get("entries", []) if isinstance(data, dict) else []
    known = set(promise_keys) if promise_keys is not None else None
    out: list[dict] = []
    seen_ids: set[str] = set()
    seen_prints: dict[str, str] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError(f"nightly waiver row is not an object: {raw!r}")
        missing = [k for k in _REQUIRED if raw.get(k) in (None, "", {})]
        entry_id = str(raw.get("id", "?"))
        if missing:
            raise ValueError(f"nightly waiver {entry_id!r} is missing {missing}")
        issue = raw["issue"]
        if not isinstance(issue, int) or isinstance(issue, bool) or issue < 1:
            raise ValueError(f"nightly waiver {entry_id!r}: issue must be a positive issue number, got {issue!r}")
        if known is not None and raw["promise"] not in known:
            raise ValueError(f"nightly waiver {entry_id!r}: unknown promise {raw['promise']!r}")
        match = raw["match"]
        if not isinstance(match, dict) or not match:
            raise ValueError(f"nightly waiver {entry_id!r}: match must be a non-empty object of mechanism fields")
        bad = sorted(set(match) - set(MATCHABLE_FIELDS))
        if bad:
            raise ValueError(
                f"nightly waiver {entry_id!r}: cannot match on {bad} (a seed or run detail is not a mechanism); "
                f"allowed: {sorted(MATCHABLE_FIELDS)}"
            )
        added = _parse_date(entry_id, "added", raw["added"])
        expires = _parse_date(entry_id, "expires", raw["expires"])
        if expires < added:
            raise ValueError(f"nightly waiver {entry_id!r}: expires {expires} is before added {added}")
        if (expires - added) > timedelta(days=MAX_LIFETIME_DAYS):
            raise ValueError(
                f"nightly waiver {entry_id!r}: lifetime {(expires - added).days} days exceeds the "
                f"{MAX_LIFETIME_DAYS}-day cap - fix the cause or renew with a new, justified entry"
            )
        if entry_id in seen_ids:
            raise ValueError(f"nightly waiver id {entry_id!r} listed twice")
        fp = _fingerprint(raw)
        if fp in seen_prints:
            raise ValueError(f"nightly waiver {entry_id!r} duplicates {seen_prints[fp]!r} (same promise and mechanism)")
        seen_ids.add(entry_id)
        seen_prints[fp] = entry_id
        out.append({**raw, "added_on": added, "expires_on": expires})
    return out


# ───────────────────────────────────────────────────────────────────────────────── issue state
class IssueStates(Protocol):
    """Where the open/closed state of an issue comes from.  Injectable so tests need no network."""

    def states(self, numbers: Iterable[int]) -> tuple[dict[int, str], list[str]]:
        """``({issue: "open"|"closed"}, problems)``; an issue that could not be resolved is a problem."""
        ...


@dataclass
class OfflineIssueStates:
    """No network.  The closed-issue check is SKIPPED, loudly (a warning per run, never a pass).

    ``known`` is an optional ``{issue: state}`` file/dict for local runs that do know the answer."""

    known: Mapping[int, str] = field(default_factory=dict)

    def states(self, numbers: Iterable[int]) -> tuple[dict[int, str], list[str]]:
        return {n: self.known[n] for n in numbers if n in self.known}, []

    @staticmethod
    def from_file(path: Path) -> OfflineIssueStates:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return OfflineIssueStates({int(k): str(v).lower() for k, v in raw.items()})


@dataclass
class GitHubIssueStates:
    """Issue state from the GitHub REST API (``GET /repos/{repo}/issues/{n}``).

    Strict by design: a failed lookup is a PROBLEM (the gate cannot prove the issue is open), and a
    number that is a pull request is rejected - a waiver is tied to an issue, not a PR.  The HTTP
    call is the one injectable seam (``fetch``) so the logic is testable with a stub."""

    repo: str
    token: str | None = None
    fetch: Callable[[str, Mapping[str, str]], dict] | None = None

    def _get(self, url: str) -> dict:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.fetch is not None:
            return self.fetch(url, headers)
        req = urllib.request.Request(url, headers=headers)  # noqa: S310 - fixed https GitHub API URL
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

    def states(self, numbers: Iterable[int]) -> tuple[dict[int, str], list[str]]:
        out: dict[int, str] = {}
        problems: list[str] = []
        for n in sorted(set(numbers)):
            try:
                body = self._get(f"https://api.github.com/repos/{self.repo}/issues/{n}")
            except (urllib.error.URLError, OSError, ValueError, KeyError) as err:
                problems.append(f"issue #{n}: could not read its state from GitHub ({type(err).__name__}: {err})")
                continue
            if "pull_request" in body:
                problems.append(f"issue #{n} is a pull request - a waiver must be tied to an issue")
                continue
            state = str(body.get("state", "")).lower()
            if state not in ("open", "closed"):
                problems.append(f"issue #{n}: unexpected state {body.get('state')!r}")
                continue
            out[n] = state
        return out, problems


def issue_states_from_args(mode: str, *, state_file: str | None, repo: str | None, token: str | None) -> IssueStates:
    """Build the lookup for the CLI: ``github`` (CI) or ``offline`` (local, optionally with a state file)."""
    if mode == "github":
        if not repo:
            raise ValueError("--issue-state github needs GITHUB_REPOSITORY (owner/name)")
        return GitHubIssueStates(repo=repo, token=token or os.environ.get("GITHUB_TOKEN"))
    if state_file:
        return OfflineIssueStates.from_file(Path(state_file))
    return OfflineIssueStates()


# ───────────────────────────────────────────────────────────────────────────────── evaluation
@dataclass
class Evaluation:
    """What the ledger made of one run.  ``clean`` is the gate."""

    unwaived: list[tuple[int, Any]] = field(default_factory=list)  # (day_seed, hit) no live entry covers
    waived: dict[str, list[tuple[int, Any]]] = field(default_factory=dict)  # entry id -> (day_seed, hit)
    problems: list[str] = field(default_factory=list)  # stale / expired / closed / ambiguous / lookup failure
    warnings: list[str] = field(default_factory=list)  # loud but not red (offline skipped the closed check)

    @property
    def clean(self) -> bool:
        return not self.unwaived and not self.problems


def matches(entry: Mapping[str, Any], promise: str, mechanism: Mapping[str, Any] | None) -> bool:
    """True when the hit is the entry's promise and EVERY listed mechanism field is equal (null matches None)."""
    if promise != entry["promise"] or mechanism is None:
        return False
    return all(k in mechanism and mechanism[k] == v for k, v in entry["match"].items())


def evaluate(
    hits: Iterable[tuple[int, Any]],
    ledger: list[dict],
    *,
    today: date,
    issue_states: IssueStates,
) -> Evaluation:
    """Judge a run's hard hits (``(day_seed, ProbeHit)``) against the ledger.  Pure apart from ``issue_states``."""
    ev = Evaluation()
    live: list[dict] = []
    states, lookup_problems = issue_states.states(e["issue"] for e in ledger)
    ev.problems.extend(lookup_problems)
    offline_unknown: list[int] = []
    for e in ledger:
        if today > e["expires_on"]:
            ev.problems.append(
                f"waiver {e['id']} (issue #{e['issue']}) expired on {e['expires_on']} - "
                "land the fix, or renew with a new justified entry"
            )
            continue
        state = states.get(e["issue"])
        if state == "closed":
            ev.problems.append(f"waiver {e['id']}: issue #{e['issue']} is closed - remove the waiver")
            continue
        if state is None and not any(str(p).startswith(f"issue #{e['issue']}") for p in lookup_problems):
            offline_unknown.append(e["issue"])
        live.append(e)
    if offline_unknown:
        ev.warnings.append(
            "OFFLINE: the closed-issue check was SKIPPED for issue(s) "
            f"{sorted(set(offline_unknown))} - open/closed state was not verified (use --issue-state github in CI)"
        )
    used: set[str] = set()
    for day_seed, hit in hits:
        covering = [e for e in live if matches(e, hit.promise, hit.mechanism)]
        if not covering:
            ev.unwaived.append((day_seed, hit))
            continue
        if len(covering) > 1:
            ev.problems.append(
                f"hit {hit.promise} (day_seed={day_seed}) is matched by {[e['id'] for e in covering]} - "
                "one mechanism must have exactly one waiver"
            )
        ev.waived.setdefault(covering[0]["id"], []).append((day_seed, hit))
        used.add(covering[0]["id"])
    for e in live:
        if e["id"] not in used:
            ev.problems.append(
                f"stale waiver {e['id']} (issue #{e['issue']}): no hit in this run matches it - "
                "the bug is fixed or this run did not reach it; remove the entry"
            )
    return ev


def format_evaluation(ev: Evaluation, ledger: list[dict]) -> list[str]:
    """Human lines for the run log (waived counts per entry, then every problem and warning)."""
    lines: list[str] = []
    by_id = {e["id"]: e for e in ledger}
    for entry_id, rows in sorted(ev.waived.items()):
        e = by_id[entry_id]
        days = len({d for d, _ in rows})
        lines.append(
            f"  waived {entry_id} (issue #{e['issue']}, expires {e['expires_on']}): "
            f"{len(rows)} hit(s) on {days} day(s) - {e['reason']}"
        )
    lines.extend(f"  WARNING: {w}" for w in ev.warnings)
    lines.extend(f"  LEDGER PROBLEM: {p}" for p in ev.problems)
    return lines
