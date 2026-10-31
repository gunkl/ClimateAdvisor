# Retirement waves: PROPOSAL ONLY (Step 2, Increment 8)

*Scribe, 2026-10-30. Nothing here is deleted or changed. Each wave needs the owner's separate approval, with revert-proof output and a coverage diff attached (user decision: retire wave by wave).*

## Standing decisions

- **U1:** FakeHass stays as the fast default engine for `tools/simulate.py` and Tier A. It is not retired in any wave.
- **Tier B (HeadlessTarry) stays frozen.** No wave touches it.
- Goldens in `tools/simulations/golden/` are LOCKED; no wave edits or removes them.
- A test is only a candidate when a real-HA test makes the same occupant claim AND `scripts/revert_proof.py` shows that real-HA test fails on the pre-fix commit. Without that, the unit test is the only guard and stays.

## Evidence required for every wave (the same checklist)

1. **Revert-proof output** for each real-HA test that replaces a unit test: `python scripts/revert_proof.py <pre-fix-commit> <tests_ha test id>` fails on the old tree and passes on HEAD. The three commands already documented in `tests_ha/test_escaped_bugs.py` cover #1108 (`b686a05^`), #1045 (`2507a19^`) and #1102 (`1df0246`).
2. **Coverage diff**: line and branch coverage of `custom_components/climate_advisor/` with the unit tier minus the candidates plus the real-HA tier, compared against unit-tier-only before the wave. No production line may lose its only covering test unless the owner accepts it by name.
3. **Mutation spot check** for each deleted test: re-introduce the bug (or revert the fix) and show the surviving suite still fails.
4. The deleted test's occupant story, copied into the wave PR, with the replacing test named.

## Per-artifact table

| Artifact | What replaces it | Deletion criterion | Evidence needed | Proposed wave |
|---|---|---|---|---|
| `FakeHass` (`tools/sim_harness/fake_hass.py`) | Nothing (U1). Real HA is a second engine for the same scenarios, not a replacement. | Never, while `tools/simulate.py` and Tier A use it. Reconsider only if the real-HA tier becomes fast enough for every-commit goldens. | n/a | none (kept) |
| `FakeScheduler` (`fake_scheduler.py`) | Nothing for FakeHass runs; real HA uses a frozen clock advanced timer by timer (`RealHASession`). | Same as FakeHass: it is the virtual clock of the fast engine. | n/a | none (kept) |
| `ha_stubs.py` | Nothing while unit tier (`tests/`, 109 files reference stubs or `install_ha_stubs`) and FakeHass need the stub layer. The real-HA tier never imports it. | Only after the unit tier and FakeHass no longer need stubbed Home Assistant, which is not in sight. Candidate to shrink, not delete. | Import graph showing zero remaining stub users | none (kept) |
| Hand-built `build_coordinator` stubs (`tools/sim_harness/build_coordinator.py` and its users; 22 files reference `build_coordinator`, 72 reference it or `object.__new__(...Coordinator)`) | The `tests_ha/` house (`make_house`) for flows that go through a real config entry, real services and real state file. | A user of the stub is retired only when a named `tests_ha` test covers the same occupant claim (checklist above). The builder itself stays while any user remains. | Per-test mapping + revert-proof + coverage diff | Wave 1 (5 tests), later waves per mapping |
| Duplicated unit tests now covered by real-HA tests | `tests_ha/test_escaped_bugs.py`, `tests_ha/test_unload_cleanup.py`, `tests_ha/test_house_lifecycle.py` | Per checklist | Per checklist | Wave 1, then 2 and 3 below |

## Wave 1 (proposed, smallest safe set): 5 tests in 3 files

Selection rule: unit tests whose occupant story is the same flow as a real-HA test that already exists and carries a revert-proof command, and which are not partially covered. Counts come from `grep` of `def test_` in each file (checked 2026-10-30).

| Unit test (file) | Replaced by | Why it is a duplicate |
|---|---|---|
| `tests/test_reload_teardown.py::test_unloaded_instance_stays_dead_after_startup_window` (the file's only test; file would be deleted) | `tests_ha/test_escaped_bugs.py::test_reloaded_zone_leaves_no_ghost` (#1102, revert-proof against `1df0246`); also `tests_ha/test_unload_cleanup.py` | Both unload a zone inside the startup window and assert the old instance runs no update, sends no command, arms no timer. The real-HA version uses real unload and setup rather than the harness's `async_unload_entry_like_ha`. |
| `tests/test_observe_only_gates.py::test_orphan_release_in_observe_only_sends_nothing_and_clears_the_pending_release` | `tests_ha/test_escaped_bugs.py::test_observe_only_does_not_release_blower` (#1108, `b686a05^`) | Same claim: with automation off, the thermostat blower CA started is not released. |
| `tests/test_observe_only_gates.py::test_orphan_release_with_automation_on_is_unchanged` | `tests_ha/test_escaped_bugs.py::test_positive_control_automation_on_releases_blower` | Control for the same flow. |
| `tests/test_fan_manual_override_restart.py::test_running_fan_with_persisted_manual_grace_is_rearmed_not_turned_off` | `tests_ha/test_escaped_bugs.py::test_manual_fan_survives_restart` (#1045, `2507a19^`) | Person turns the fan on, HA restarts, the fan is not turned off. |
| `tests/test_fan_manual_override_restart.py::test_expired_while_down_is_clean_slate` | `tests_ha/test_escaped_bugs.py::test_positive_control_expired_manual_fan_is_turned_off_after_restart` | Same expiry-while-down control. |

Left in place on purpose (not duplicated, or only partly): the other 6 tests in `test_observe_only_gates.py` (nudge retry, `_call_climate` dry-run outcome, renderer text, drift-off command: the real-HA tests cover the blower and thermostat-command claims only), the other 12 in `test_fan_manual_override_restart.py` (activity-event tagging, shadow FSM feeding, sealed-house, RF token precedence, status text), all 11 in `test_reload_lifecycle.py` (latch, idempotent shutdown, boot hooks: finer-grained than the real-HA tests), and `tests/test_teardown_static.py` (static guards, not covered by any runtime test). Together they are the "mirror" detail the real-HA tier does not reproduce.

Wave 1 net effect: -5 unit tests, -1 file. The risk is small and it proves the process before larger waves.

## Later waves (sketch, not requested yet)

- **Wave 2:** after Wave 1 is accepted, map the rest of `test_fan_manual_override_restart.py` and `test_observe_only_gates.py` one test at a time; retire only those with a real-HA twin. Likely outcome: most stay, because they pin activity-log wording and engine-internal gates the real-HA tier does not read.
- **Wave 3:** evaluate `tests/test_reload_lifecycle.py` against `tests_ha/test_house_lifecycle.py` and `tests_ha/test_unload_cleanup.py` (11 unit tests vs 4 and 2 real-HA tests, counted from `def test_`). Needs a per-test map first.
- **Not proposed for any wave:** golden scenarios, `test_production_harness.py` (Tier A), the sim_harness unit tests, `tests/test_service_call_choke_points.py`, `tests/test_teardown_static.py`.

## Unverified

- The "same claim" mapping above is by reading test names and docstrings, not by running both tests under mutation. The revert-proof and coverage diff are exactly the evidence that would confirm or reject each pairing; if one fails, that pair drops out of Wave 1.
- The file counts for stub users (109, 22, 72) are raw `grep -l` counts over `tests/` and include files that only mention the name.
- I did not run `scripts/revert_proof.py` (it checks out old trees and runs the real-HA tier under WSL; not run while other agents edit code).
