"""Verification script for the compressor deadband/dwell decision used by
SimulatedThermostat (climate.py::_decide_compressor_state, and the
_reset_compressor_state() mode-change guard around it).

NOT a pytest test — plain script, run directly: `python test_compressor_state.py`.
Same rationale as test_sim_math.py: climate.py imports `homeassistant.*` at module
level, and no `homeassistant` package exists in this repo/venv, so the real function
can't be imported here. This hand-transcribes the decision logic from climate.py
(read in full, not just the signature) for verification only — climate.py itself
always calls the real function; this file is never imported by it.

Root cause under test (Issue #970-adjacent fix, no issue number captured for this
change — test-fixture-only, no version bump): `_async_tick()`'s compressor on/off
decision used to branch on `self._compressor_on`/`self._last_on_ts`/`self._last_off_ts`
carried over from whatever HVAC mode was previously active. Climate Advisor's real
automation always switches a zone's hvac_mode via a single bundled
`climate.set_temperature` call (never a separate `climate.set_hvac_mode`, and rarely
routes through OFF between heat and cool) — so a direct heat<->cool transition let the
new mode's compressor start "on" without ever crossing its own turn-on deadband, and
computed min_run/min_off dwell against the old mode's wall-clock reference. The fix
resets compressor_on/last_on_ts/last_off_ts to (False, None, None) at both call sites
that change hvac_mode, whenever the mode actually changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _decide_compressor_state(
    *,
    mode: str | None,
    current_temp: float,
    target_temp: float | None,
    deadband: float | None,
    compressor_on: bool,
    last_on_ts: datetime | None,
    last_off_ts: datetime | None,
    min_run_seconds: float,
    min_off_seconds: float,
    now: datetime,
) -> tuple[bool, datetime | None, datetime | None]:
    """Hand-transcribed copy of climate.py::_decide_compressor_state — see its
    docstring there for the authoritative version. Verified identical by inspection."""
    if mode is None:
        return False, last_on_ts, last_off_ts
    if target_temp is None or deadband is None:
        return compressor_on, last_on_ts, last_off_ts

    if not compressor_on:
        wants_on = (mode == "heat" and current_temp <= target_temp - deadband) or (
            mode == "cool" and current_temp >= target_temp + deadband
        )
        can_turn_on = last_off_ts is None or (now - last_off_ts).total_seconds() >= min_off_seconds
        if wants_on and can_turn_on:
            return True, now, last_off_ts
        return compressor_on, last_on_ts, last_off_ts

    wants_off = (mode == "heat" and current_temp >= target_temp + deadband) or (
        mode == "cool" and current_temp <= target_temp - deadband
    )
    can_turn_off = last_on_ts is None or (now - last_on_ts).total_seconds() >= min_run_seconds
    if wants_off and can_turn_off:
        return False, last_on_ts, now
    return compressor_on, last_on_ts, last_off_ts


def _reset_compressor_state() -> tuple[bool, None, None]:
    """Hand-transcribed copy of SimulatedThermostat._reset_compressor_state()."""
    return False, None, None


_FAILURES: list[str] = []


def _check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        _FAILURES.append(label)


NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

# --- Case 1: heat mode, compressor on, direct switch to cool -----------------
# Before the fix: switching hvac_mode straight from heat to cool while the heat
# compressor was on carried compressor_on=True into cool's threshold checks,
# skipping cool's own turn-on deadband entirely.
compressor_on, last_on_ts, last_off_ts = True, NOW - timedelta(minutes=10), None

# The mode-change call sites (async_set_hvac_mode / async_set_temperature) reset
# state before the next tick evaluates the new mode.
compressor_on, last_on_ts, last_off_ts = _reset_compressor_state()
_check("reset clears compressor_on", compressor_on is False)
_check("reset clears last_on_ts", last_on_ts is None)
_check("reset clears last_off_ts", last_off_ts is None)

# Next tick: cool mode, target=74, deadband=1.5 (turn-on threshold = 75.5).
# Indoor is 68 — nowhere near cool's turn-on threshold — so compressor must stay off.
compressor_on, last_on_ts, last_off_ts = _decide_compressor_state(
    mode="cool",
    current_temp=68.0,
    target_temp=74.0,
    deadband=1.5,
    compressor_on=compressor_on,
    last_on_ts=last_on_ts,
    last_off_ts=last_off_ts,
    min_run_seconds=300.0,
    min_off_seconds=300.0,
    now=NOW,
)
_check("cool stays off after heat->cool switch when below cool's turn-on threshold", compressor_on is False)

# Indoor rises to 76 (>= 75.5) — now cool's own turn-on deadband is legitimately crossed.
compressor_on, last_on_ts, last_off_ts = _decide_compressor_state(
    mode="cool",
    current_temp=76.0,
    target_temp=74.0,
    deadband=1.5,
    compressor_on=compressor_on,
    last_on_ts=last_on_ts,
    last_off_ts=last_off_ts,
    min_run_seconds=300.0,
    min_off_seconds=300.0,
    now=NOW,
)
_check("cool turns on once its own turn-on deadband is crossed", compressor_on is True)
_check("last_on_ts set to the turn-on tick's `now`, not the stale heat-mode timestamp", last_on_ts == NOW)

# --- Case 2: cool mode, compressor on, direct switch to heat (symmetric case) -
compressor_on, last_on_ts, last_off_ts = True, NOW - timedelta(minutes=10), None
compressor_on, last_on_ts, last_off_ts = _reset_compressor_state()

# Next tick: heat mode, target=68, deadband=1.5 (turn-on threshold = 66.5).
# Indoor is 70 — nowhere near heat's turn-on threshold — so compressor must stay off.
compressor_on, last_on_ts, last_off_ts = _decide_compressor_state(
    mode="heat",
    current_temp=70.0,
    target_temp=68.0,
    deadband=1.5,
    compressor_on=compressor_on,
    last_on_ts=last_on_ts,
    last_off_ts=last_off_ts,
    min_run_seconds=300.0,
    min_off_seconds=300.0,
    now=NOW,
)
_check("heat stays off after cool->heat switch when above heat's turn-on threshold", compressor_on is False)

# Indoor falls to 66 (<= 66.5) — heat's own turn-on deadband is legitimately crossed.
compressor_on, last_on_ts, last_off_ts = _decide_compressor_state(
    mode="heat",
    current_temp=66.0,
    target_temp=68.0,
    deadband=1.5,
    compressor_on=compressor_on,
    last_on_ts=last_on_ts,
    last_off_ts=last_off_ts,
    min_run_seconds=300.0,
    min_off_seconds=300.0,
    now=NOW,
)
_check("heat turns on once its own turn-on deadband is crossed", compressor_on is True)
_check("last_on_ts set fresh on the new mode's turn-on tick", last_on_ts == NOW)

# --- Case 3: dwell timers don't spuriously block the new mode's first transition
# Without the reset, a stale last_off_ts from the old mode could hold min_off_seconds
# against the new mode's turn-on for no physical reason. After reset, last_off_ts is
# None, so can_turn_on is immediately True regardless of min_off_seconds.
compressor_on, last_on_ts, last_off_ts = _reset_compressor_state()
compressor_on, last_on_ts, last_off_ts = _decide_compressor_state(
    mode="cool",
    current_temp=90.0,  # far past any reasonable turn-on threshold
    target_temp=74.0,
    deadband=1.5,
    compressor_on=compressor_on,
    last_on_ts=last_on_ts,
    last_off_ts=last_off_ts,
    min_run_seconds=300.0,
    min_off_seconds=1800.0,  # a long min_off_seconds that would matter if last_off_ts weren't cleared
    now=NOW,
)
_check("cleared last_off_ts does not block the new mode's first turn-on", compressor_on is True)

# --- Case 4: pre-fix regression guard — same scenario WITHOUT the reset ------
# Demonstrates the bug this fix removes. Cool mode, target=74, deadband=1.5: a fresh
# (off) compressor only turns on at current_temp >= 75.5, and only turns back off at
# current_temp <= 72.5 — so 72.5 < current_temp < 75.5 is a dead zone a freshly-off
# compressor never enters. current_temp=74.0 sits inside that dead zone: a correctly
# reset (off) state stays off here, but a stale compressor_on=True carried over
# unreset from a prior heat cycle sees wants_off = False at this temp and stays on —
# heat capacity's own turn-on deadband was bypassed entirely (the `if not
# compressor_on:` branch that contains it never ran).
stale_compressor_on, stale_last_on_ts, stale_last_off_ts = True, NOW - timedelta(minutes=10), None
buggy_result, _, _ = _decide_compressor_state(
    mode="cool",
    current_temp=74.0,  # inside the dead zone (72.5, 75.5) — a fresh-off compressor would never turn on here
    target_temp=74.0,
    deadband=1.5,
    compressor_on=stale_compressor_on,  # carried over from heat mode, unreset
    last_on_ts=stale_last_on_ts,
    last_off_ts=stale_last_off_ts,
    min_run_seconds=300.0,
    min_off_seconds=300.0,
    now=NOW,
)
_check(
    "regression guard: without the reset, stale compressor_on=True bypasses cool's turn-on deadband "
    "(demonstrates the bug the fix removes — compressor incorrectly stays 'on' in the dead zone)",
    buggy_result is True,
)
correct_result, _, _ = _decide_compressor_state(
    mode="cool",
    current_temp=74.0,
    target_temp=74.0,
    deadband=1.5,
    compressor_on=False,  # correctly reset
    last_on_ts=None,
    last_off_ts=None,
    min_run_seconds=300.0,
    min_off_seconds=300.0,
    now=NOW,
)
_check("with the reset, a fresh-off compressor correctly stays off in the same dead zone", correct_result is False)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} check(s) FAILED:")
    for f in _FAILURES:
        print(f"  - {f}")
    raise SystemExit(1)
print("All checks passed.")
