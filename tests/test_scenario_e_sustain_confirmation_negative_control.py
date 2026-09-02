"""Negative control for Issue #821 Scenario E (Verification BLOCKING #2).

Verification (Opus, independent review) found sustain-confirmation provably
decorative: patching `_confirm_nat_vent_exit_candidate()` to `return True` immediately
(the exact pre-#821 instantaneous-commit behavior) and rerunning the full suite
produced zero failures — the 5 retimed golden scenarios pass identically with and
without the mechanism, because retiming (adding a later confirming tick) removed the
only signal that would have caught its deletion.

This file loads the real scenario E JSON
(``tools/simulations/pending/issue_821_scenario_e_sustain_confirmation_noise_rejection.json``)
and runs it through the real production harness twice:

1. Normally — confirming nat-vent survives two flickers of the PROACTIVE_FLOOR exit
   condition, matching the JSON scenario's own assertions (also covered by
   ``python tools/simulate.py --pending``).
2. With ``AutomationEngine._confirm_nat_vent_exit_candidate()`` patched to commit
   immediately (bypassing the sustain-confirmation clock entirely) — proving the SAME
   scenario's session does NOT survive the very first flicker (08:10:00) once the
   mechanism is removed. This is the negative control the Issue #821 plan's Design §0
   required and that shipped without it.

This is a genuine revert-test, not a restatement of the scenario's own JSON
assertions — test 2 below is provably impossible to pass without sustain-confirmation
actually gating the exit commit, and was confirmed (by the Executor, manually, via
``pytest.main()`` under the patch) to fail against the current, unpatched code before
being finalized.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from tools.sim_harness.run_production import run_production_scenario  # noqa: E402

_SCENARIO_PATH = (
    REPO_ROOT / "tools" / "simulations" / "pending" / "issue_821_scenario_e_sustain_confirmation_noise_rejection.json"
)


def _load_scenario() -> dict:
    return json.loads(_SCENARIO_PATH.read_text(encoding="utf-8"))


def test_scenario_e_survives_noise_with_sustain_confirmation_present():
    """Sanity check mirroring the JSON scenario's own assertions — nat-vent is still
    active at the end of the scenario (both flickers of the PROACTIVE_FLOOR condition
    were rejected as unsustained noise)."""
    scenario = _load_scenario()
    result = run_production_scenario(scenario)

    assert result.engine_state.get("_natural_vent_active") is True, (
        "With sustain-confirmation present, the session must survive both noise flickers "
        f"— final engine_state: {result.engine_state}"
    )


def test_negative_control_without_sustain_confirmation_the_noise_commits_the_exit():
    """THE negative control: patches _confirm_nat_vent_exit_candidate() to commit
    immediately (candidate is None or exempt=True path — return True unconditionally,
    exactly Verification's own revert-test), reruns the identical scenario, and
    confirms the FIRST flicker (08:10:00) now ends the session — proving
    sustain-confirmation, not some other mechanism, is what scenario E's own
    'nat_vent_still_active' assertion actually depends on.
    """
    scenario = _load_scenario()

    with patch.object(AutomationEngine, "_confirm_nat_vent_exit_candidate", return_value=True):
        result = run_production_scenario(scenario)

    assert result.engine_state.get("_natural_vent_active") is False, (
        "Negative control: with sustain-confirmation disabled, the very first noisy "
        "PROACTIVE_FLOOR flicker at 08:10:00 must commit the exit immediately (the exact "
        "pre-Issue #821 instantaneous-commit bug) — if this assertion fails, either the "
        "patch didn't actually disable the mechanism, or scenario E doesn't exercise it. "
        f"final engine_state: {result.engine_state}"
    )
