"""Issue #641 prevention: registry-enforced coverage of every ``_exit_nat_vent()`` call
site's reactivation-lockout arming decision.

The WHF fast-cycling incident's root cause was a scoping decision made once, at Issue
#411/#608, that was correct for 3 of 4 reactivation-gate call sites and silently wrong
for a 4th (``PROACTIVE_FLOOR``) — nothing forced a re-check of that assumption when new
exit reasons were added later. During this fix's own audit, two more previously-unnoticed
sibling gaps surfaced (``CEILING_THRESHOLD`` and ``fan_thermostat_check()``'s
``STOP_DEACTIVATE`` branch) purely from reading every call site by hand.

This module is the durable fix for "how do we know this scoping decision stays correct
as new exit reasons get added": it AST-scans ``automation.py`` for every
``self._exit_nat_vent(...)`` call and requires each to be explicitly classified in
``_COVERAGE_REGISTRY`` as ``"arms lockout"`` or ``"exempted: <reason>"`` — the same
enforcement shape as ``tests/test_shadow_engine_coverage.py``'s ``_TRACKED_FIELDS``
registry and ``tests/test_executor_offload.py``'s ``_BLOCKING_METHODS`` registry. A new
call site added later without a classification fails immediately, instead of silently
repeating this bug class a fourth time.

**Known blind spot (Issue #739):** this scanner only sees calls to
``self._exit_nat_vent(...)`` — a branch that bypasses that function entirely is invisible
to it. ``check_natural_vent_conditions()``'s own ``COMFORT_FLOOR`` branch does exactly
that (it calls ``_deactivate_fan()`` directly, per that branch's own Issue #620 note), and
it silently omitted arming ``_nat_vent_outdoor_exit_time`` for the same reason every other
exit reason here needs it. There is no registry entry for this site since it makes no
``_exit_nat_vent()`` call to key on; coverage instead lives in a direct unit test —
``tests/test_fan_control.py::TestNatVentComfortFloorExit::test_comfort_floor_exit_arms_reactivation_lockout``
— mirroring how Issue #755's harness-unreachable ``STOP_COOLED_TO_FLOOR`` fix was verified.
If another bypass-style exit branch (calls ``_deactivate_fan()``/sets
``_natural_vent_active = False`` directly, without going through ``_exit_nat_vent()``) is
ever added, it needs its own such test — this scanner structurally cannot catch it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_AUTOMATION_PY = _REPO_ROOT / "custom_components" / "climate_advisor" / "automation.py"

# Key: (enclosing method name, 1-based ordinal of the _exit_nat_vent() call within that
# method's source order). Ordinal keying (not line numbers) stays stable across edits
# elsewhere in the file, matching this project's documented convention of treating line
# numbers as a navigation aid, not a contract (nat-vent-lifecycle-spec.md).
#
# Classification meanings:
#   "arms lockout"   — passes set_outdoor_exit_time=True. Required whenever the exit
#                       reason's own condition is NOT self-complementary with the
#                       instant reactivation gate's condition at a fixed indoor/outdoor
#                       reading — i.e. the exit and re-entry conditions could both be
#                       satisfied by the same (or a barely-changed) reading, the way
#                       PROACTIVE_FLOOR's independent time-to-floor prediction can.
#   "exempted: <reason>" — deliberately does not arm the lockout, with a specific reason
#                       (never a bare "not needed").
_COVERAGE_REGISTRY: dict[tuple[str, int], str] = {
    ("handle_all_doors_windows_closed", 1): (
        "exempted: fires only when _any_monitored_sensor_open() is False (all sensors just "
        "closed) — _exit_nat_vent() always takes the sensors-closed grace-period branch here, "
        "which never consults the paused-reactivation lockout at all"
    ),
    ("check_natural_vent_conditions", 1): (
        "arms lockout — Issue #641 (PROACTIVE_FLOOR): a predictive time-to-floor check "
        "computed independently of the instant reactivation gate; almost always still "
        "satisfied on the very next tick without this lockout (the reported incident)"
    ),
    ("check_natural_vent_conditions", 2): (
        "arms lockout — original outdoor-rise exit (Issue #115/#411), the lockout mechanism's "
        "first and originally-only use case"
    ),
    ("check_natural_vent_conditions", 3): (
        "arms lockout — Issue #641 (CEILING_THRESHOLD): outdoor hovering near "
        "comfort_cool + nat_vent_delta can cross the boundary tick-to-tick from sensor/weather "
        "noise, flip-flopping against the reactivation gate's own outdoor < threshold check"
    ),
    ("nat_vent_temperature_check", 1): (
        "exempted: guarded by an explicit `not self._any_monitored_sensor_open()` "
        "precondition (session force-closed because sensors are already closed) — always "
        "takes the sensors-closed grace-period branch, lockout never consulted"
    ),
    ("nat_vent_temperature_check", 2): (
        "exempted: this ordinal is ONE shared _exit_nat_vent() call site serving all 4 of "
        "this function's exit reasons (COMFORT_FLOOR/PROACTIVE_FLOOR/OUTDOOR_RISE/"
        "CEILING_THRESHOLD) via a per-branch local (`_set_outdoor_exit_time`), not a literal "
        "at the call site — so `_find_calls_with_lockout_flag()`'s literal-True check "
        "structurally cannot confirm arming here for ANY of the 4 reasons, not just this one "
        "(verified by direct reading, not by this positive control). 3 of the 4 branches "
        "(PROACTIVE_FLOOR/OUTDOOR_RISE/CEILING_THRESHOLD) already set that local True. Issue "
        "#696: COMFORT_FLOOR (previously the one holdout, on the now-disproven claim that "
        "exit and re-entry check the same quantity at a fixed reading and so can't both be "
        "satisfied by the same indoor temperature — false once indoor drifts across the floor "
        "between ticks, as it did in production on 2026-08-23) now sets it True too — see the "
        "COMFORT_FLOOR branch in `nat_vent_temperature_check()` directly, and "
        "`tools/simulations/pending/issue_696_idle_open_reactivation_bypasses_lockout.json` "
        "for the regression scenario."
    ),
    ("fan_thermostat_check", 1): (
        "arms lockout — STOP_VIA_NAT_VENT_EXIT, the tick-level twin of "
        "check_natural_vent_conditions()'s OUTDOOR_RISE exit (same boundary condition, "
        "different dispatch path); already correctly armed since Issue #411/#418"
    ),
    ("fan_thermostat_check", 2): (
        "arms lockout — Issue #641: STOP_DEACTIVATE (Check 1's non-nat-vent-specific "
        "direction-reversal stop) is documented in its own comment as 'the exact same "
        "boundary condition' as STOP_VIA_NAT_VENT_EXIT above, just without the "
        "nat-vent-active gate — found unarmed during this issue's own coverage audit"
    ),
    ("fan_thermostat_check", 3): (
        "arms lockout — Issue #755: STOP_COOLED_TO_FLOOR (Check 2) previously exempted on "
        "the same self-complementary-floor reasoning as nat_vent_temperature_check's "
        "hard-floor exit — disproven by Issue #696's live incident (indoor drifts across "
        "the floor between ticks). This tick-level check runs on every sensor update, a "
        "higher-frequency trigger than #696's, so now sets set_outdoor_exit_time=True too."
    ),
    ("_reconcile_fan_on_startup_locked", 1): (
        "arms lockout — Issue #790: previously exempted on the same 'runs at most once per "
        "restart/30-min backstop' claim disproven for this method's check-side bypass (see "
        "nat_vent_reactivation_lockout.py's module docstring) — 2 of this method's 4 real "
        "triggers (thermostat_state_change, post_grace_expiry) are event-driven and can fire "
        "sub-minute. Without this, a turn-off issued from this call site left no lockout "
        "timer for a subsequent reconcile call to check."
    ),
    ("on_fan_turned_off", 1): (
        "exempted: fires once per real fan-off state-change event (RF timer boundary settle "
        "reconciliation), not a periodic re-evaluation — cannot self-trigger repeatedly the "
        "way a per-tick predictive/instant check can; any resulting reactivation attempt is "
        "separately covered by the Issue #641 rate-limit backstop (Part B, "
        "_fan_toggle_rate_limited())"
    ),
}


def _find_exit_nat_vent_call_sites() -> list[tuple[str, int]]:
    """AST-scan AutomationEngine for every ``self._exit_nat_vent(...)`` call, keyed by
    (enclosing method name, 1-based ordinal within that method's source order)."""
    tree = ast.parse(_AUTOMATION_PY.read_text(encoding="utf-8"))
    engine_class = next(
        node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "AutomationEngine"
    )
    sites: list[tuple[str, int]] = []
    for node in engine_class.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        calls = [
            sub
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "_exit_nat_vent"
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "self"
        ]
        calls.sort(key=lambda c: (c.lineno, c.col_offset))
        for ordinal, _call in enumerate(calls, start=1):
            sites.append((node.name, ordinal))
    return sites


def _find_calls_with_lockout_flag() -> dict[tuple[str, int], bool]:
    """Same scan as above, but also records whether each call passes
    set_outdoor_exit_time=True — lets the registry's "arms lockout" claim be checked
    against the actual code, not just trusted."""
    tree = ast.parse(_AUTOMATION_PY.read_text(encoding="utf-8"))
    engine_class = next(
        node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "AutomationEngine"
    )
    result: dict[tuple[str, int], bool] = {}
    for node in engine_class.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        calls = [
            sub
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "_exit_nat_vent"
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "self"
        ]
        calls.sort(key=lambda c: (c.lineno, c.col_offset))
        for ordinal, call in enumerate(calls, start=1):
            arms = any(
                kw.arg == "set_outdoor_exit_time" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                for kw in call.keywords
            )
            result[(node.name, ordinal)] = arms
    return result


class TestExitNatVentLockoutCoverageRegistry:
    def test_every_call_site_is_registered(self) -> None:
        found = set(_find_exit_nat_vent_call_sites())
        unregistered = found - set(_COVERAGE_REGISTRY)
        assert not unregistered, (
            f"New _exit_nat_vent() call site(s) found but not classified in "
            f"_COVERAGE_REGISTRY: {sorted(unregistered)}. Add each as "
            f'"arms lockout" or "exempted: <reason>" — see Issue #641. Ask: could this '
            f"exit reason's own condition be re-satisfied by the reactivation gate on the "
            f"very next tick, at the same or barely-changed indoor/outdoor reading?"
        )

    def test_registry_has_no_stale_entries(self) -> None:
        """Catches a registry entry left behind after its call site was removed or
        moved to a different method — a stale entry would otherwise silently hide a
        real coverage gap forever."""
        found = set(_find_exit_nat_vent_call_sites())
        stale = set(_COVERAGE_REGISTRY) - found
        assert not stale, f"Registry references call site(s) that no longer exist: {sorted(stale)}. Update it."

    def test_arms_lockout_claims_match_the_actual_code(self) -> None:
        """Positive control: every registry entry claiming "arms lockout" must actually
        pass set_outdoor_exit_time=True in the real code, and every entry NOT claiming
        it must NOT pass it — catches the registry drifting from reality in either
        direction (a claimed-but-unarmed site would silently ship this bug class again;
        an armed-but-unclaimed site means the registry's own documentation is wrong)."""
        actual = _find_calls_with_lockout_flag()
        for key, classification in _COVERAGE_REGISTRY.items():
            claims_armed = classification.startswith("arms lockout")
            really_armed = actual.get(key)
            assert really_armed is not None, f"{key} in registry but not found by the call-site scan"
            assert really_armed == claims_armed, (
                f"{key} registry classification says "
                f"{'armed' if claims_armed else 'exempted'} but the actual code "
                f"{'arms' if really_armed else 'does not arm'} the lockout — registry and "
                f"code have drifted apart"
            )
