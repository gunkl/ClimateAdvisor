"""mechanism - the mechanism fields of a promise hit (Issue #1153).

A nightly seed is fresh every run, so a known hit must be recognised by the MECHANISM that produced
it, never by its seed.  This module turns one production ``Hit`` plus the journal window it was
judged on into a flat, always-complete record of that mechanism.  It owns no decision logic: every
value is READ from where production already records it, and a value that cannot be known for a given
hit is an explicit ``None`` (never omitted, so a ledger match on ``null`` is a real statement).

Sources (all read-only):

* command service / target / mode / origin  <- the journal ``Command`` (``promise_journal.Command``)
* cause                                     <- ``Command.cause``, which production fills from the engine's
                                               ``_mode_change_reason`` (``coordinator._promise_observe_command``)
* command reason                            <- the ``reason`` label of the ``thermostat_mode_set`` event CA
                                               emitted for that command (``AutomationEngine._emit_thermostat_mode_set``)
* episode kind / source / grace seconds     <- the derived ``Episode`` and the ``Mark`` attrs (``grace_started``
                                               ``source`` / ``duration_seconds``, ``override_detected`` ``source``)
* configured grace seconds                  <- the run's own config (``manual_grace_seconds``)
* override / grace FSM state                <- the engine's flags, sampled by the source adapter when the hit
                                               was first detected (``ctx``)
* sensor open                               <- the journal ``Reading.contacts_open`` at the hit
* commit                                    <- ``GITHUB_SHA`` in CI, else ``git rev-parse HEAD``
"""

from __future__ import annotations

import functools
import os
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.sim_harness.ha_stubs import install_ha_stubs

# The production promise modules live inside the climate_advisor package, whose __init__ needs
# Home Assistant; under the harness that is the stub layer (idempotent).
install_ha_stubs()

from custom_components.climate_advisor.promise_journal import (  # noqa: E402
    Command,
    Episode,
    Mark,
    PromiseInput,
    Reading,
    mark_attr,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# The nearest preceding CA command is "the" command of a hit when it went out within one probe tick
# (the five-minute coordinator tick); an older command says nothing about this hit.
COMMAND_LOOKBACK_S = 300.0

# FSM flags read from the engine by the source adapter (attribute name -> mechanism key suffix).
FSM_FLAGS: tuple[tuple[str, str], ...] = (
    ("_manual_override_active", "manual_override_active"),
    ("_override_confirm_pending", "override_confirm_pending"),
    ("_grace_active", "grace_active"),
    ("_fan_override_active", "fan_override_active"),
    ("_paused_by_door", "paused_by_door"),
    ("_natural_vent_active", "natural_vent_active"),
)

# Fields that identify a mechanism (a ledger may match on these).  ``commit`` and ``command_age_s``
# describe THIS run, not the mechanism, so they are recorded but never matchable.
MATCHABLE_FIELDS: tuple[str, ...] = (
    "cause",
    "command_service",
    "command_target",
    "command_mode",
    "command_reason",
    "command_origin",
    "episode_kind",
    "episode_source",
    "episode_grace_seconds",
    "episode_end_reason",
    "configured_grace_seconds",
    *(f"fsm_{suffix}" for _, suffix in FSM_FLAGS),
    "sensor_open",
)
RECORDED_ONLY_FIELDS: tuple[str, ...] = ("command_age_s", "commit")
MECHANISM_FIELDS: tuple[str, ...] = MATCHABLE_FIELDS + RECORDED_ONLY_FIELDS


@functools.lru_cache(maxsize=1)
def current_commit() -> str | None:
    """The commit under test: ``GITHUB_SHA`` in CI, else the checkout's HEAD; ``None`` when unknown."""
    sha = os.environ.get("GITHUB_SHA", "").strip()
    if sha:
        return sha
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    sha = out.stdout.strip()
    return sha or None


def fingerprint(mechanism: Mapping[str, Any] | None) -> str:
    """The matchable, non-null fields of a mechanism as one stable line (for logs and grouping hits)."""
    if not mechanism:
        return "(no mechanism)"
    return " ".join(f"{k}={mechanism[k]}" for k in MATCHABLE_FIELDS if mechanism.get(k) is not None) or "(all null)"


def blank_mechanism(commit: str | None = None) -> dict[str, Any]:
    """Every field present, every value ``None`` (the shape a ledger and a reader can rely on)."""
    out: dict[str, Any] = dict.fromkeys(MECHANISM_FIELDS)
    out["commit"] = commit
    return out


def _command_for(hit_ts: datetime, hit_target: Any, commands: tuple[Command, ...]) -> Command | None:
    """The latest CA command at or before the hit (same device when the hit names one) inside the lookback."""
    best: Command | None = None
    for c in commands:
        if c.origin != "ca" or c.ts > hit_ts:
            continue
        if hit_target in ("thermostat", "fan") and c.target != hit_target:
            continue
        if (hit_ts - c.ts).total_seconds() > COMMAND_LOOKBACK_S:
            continue
        if best is None or c.ts >= best.ts:
            best = c
    return best


def _episode_for(hit_ts: datetime, hit_target: Any, episodes: tuple[Episode, ...]) -> Episode | None:
    """The newest episode on the hit's device that was open at the hit (an episode ending AT the hit counts)."""
    best: Episode | None = None
    for e in episodes:
        if hit_target in ("thermostat", "fan") and e.target != hit_target:
            continue
        if e.start_ts > hit_ts or (e.end_ts is not None and e.end_ts < hit_ts):
            continue
        if best is None or e.start_ts >= best.start_ts:
            best = e
    return best


def _episode_marks(ep: Episode, marks: tuple[Mark, ...]) -> tuple[Mark | None, Mark | None]:
    """(the mark that opened the episode, the grace_started mark that set its protected window)."""
    opener = next(
        (
            m
            for m in marks
            if m.ts == ep.start_ts and m.kind in ("override_detected", "fan_manual_override", "grace_started")
        ),
        None,
    )
    grace: Mark | None = None
    for m in marks:
        if m.kind != "grace_started" or m.ts < ep.start_ts or (ep.end_ts is not None and m.ts > ep.end_ts):
            continue
        trigger = str(mark_attr(m, "trigger") or "")
        if ep.kind == "fan_off":
            matches = trigger == "fan_off"
        else:
            matches = mark_attr(m, "source") == "manual" and (("fan" in trigger) == (ep.target == "fan"))
        if matches:
            grace = m
            break
    return opener, grace


def _mode_set_reason(cmd: Command, events: Sequence[tuple[datetime, Mapping[str, Any]]]) -> str | None:
    """The ``reason`` label of the ``thermostat_mode_set`` event CA emitted for this very command.

    Only a thermostat mode command has one (CA announces real mode CHANGES, once per change), and it is
    emitted at the same instant as the command.  Anything else is ``None`` - not knowable, not guessed.
    """
    if cmd.target != "thermostat" or cmd.service != "set_hvac_mode":
        return None
    for ts, payload in reversed(events):
        if ts == cmd.ts and payload.get("to_mode") == cmd.mode:
            reason = payload.get("reason")
            return str(reason) if reason is not None else None
    return None


def _reading_at(hit_ts: datetime, readings: tuple[Reading, ...]) -> Reading | None:
    best: Reading | None = None
    for r in readings:
        if r.ts <= hit_ts and (best is None or r.ts >= best.ts):
            best = r
    return best


def build_mechanism(
    hit: Any,
    inp: PromiseInput,
    *,
    ctx: Mapping[str, Any] | None = None,
    mode_set_events: Sequence[tuple[datetime, Mapping[str, Any]]] = (),
    commit: str | None = None,
) -> dict[str, Any]:
    """The mechanism record of ``hit`` (a production ``Hit``) judged on ``inp``.  Pure; never raises."""
    out = blank_mechanism(commit)
    ctx = ctx or {}
    target = dict(hit.evidence).get("target")
    cmd = _command_for(hit.ts, target, inp.commands)
    if cmd is not None:
        out["cause"] = cmd.cause
        out["command_service"] = cmd.service
        out["command_target"] = cmd.target
        out["command_mode"] = cmd.mode
        out["command_origin"] = cmd.origin
        out["command_reason"] = _mode_set_reason(cmd, mode_set_events)
        out["command_age_s"] = round((hit.ts - cmd.ts).total_seconds(), 1)
    ep = _episode_for(hit.ts, target, inp.episodes)
    if ep is not None:
        opener, grace = _episode_marks(ep, inp.marks)
        out["episode_kind"] = ep.kind
        out["episode_end_reason"] = ep.end_reason
        source = mark_attr(grace, "source") if grace is not None else None
        if source is None and opener is not None:
            source = mark_attr(opener, "source")
        out["episode_source"] = source
        seconds = mark_attr(grace, "duration_seconds") if grace is not None else None
        out["episode_grace_seconds"] = seconds
    out["configured_grace_seconds"] = ctx.get("configured_grace_seconds")
    for _, suffix in FSM_FLAGS:
        out[f"fsm_{suffix}"] = ctx.get(f"fsm_{suffix}")
    reading = _reading_at(hit.ts, inp.readings)
    out["sensor_open"] = reading.contacts_open if reading is not None else None
    return out
