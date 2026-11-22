Fixes #<issue>  <!-- bug fix: the bug issue. Feature or enhancement: an issue labelled `approach agreed`. -->

## What changes for the person at home
<!-- Before → after, in plain words. -->

## Evidence
<!-- The data this rests on: diagnostics, logs, times, a simulation reproduction. Or "hypothesis only", with what would confirm it. -->

## Root cause and origin
<!-- Regression (name the commit or PR) or original design gap, and how you found it (git log -S ...). -->

## States covered
<!-- A table of the conditions involved, today vs with this change, including edge cases and controls. -->

## Blast radius
<!-- Every writer and reader of the changed symbols across the repository, and why each is fine. -->

## Mirror check
<!-- The opposite path you checked (start/stop, open/close, enter/exit), and what you found. -->

## Verification
- [ ] New tests fail with the fix reverted and pass with it
- [ ] Controls pass for the right reason
- [ ] `python scripts/test.py unit` (and `ha` if lifecycle, listeners, timers or persistence changed)
- [ ] `python tools/simulate.py` for behaviour changes; generated days for the area
- [ ] `ruff check .`, `ruff format --check .`, `python tools/validate.py`

## Housekeeping
- [ ] Version bumped (`const.py`, `manifest.json`), `CHANGELOG.md`, `fix_history.jsonl` via `tools/add_fix_entry.py`
- [ ] Docs updated where behaviour changed; new occupant-facing strings in every language
- [ ] No golden or `tools/sim_harness/outcomes.py` change without the maintainer's approval

<!-- Small changes (typos, links, translations) only need the first section. See CONTRIBUTING.md. -->
