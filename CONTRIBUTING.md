# Contributing to Climate Advisor

Climate Advisor runs real heating, cooling and fans in someone's home. Every change is judged by what the person living there experiences: comfort, cost and trust that the house does what the briefing says. This guide collects the project's existing rules in one place. Where a rule is written down in more detail elsewhere, the section links to it; that document stays the authority.

Contents: [Branches](#branches-and-where-a-change-goes) · [Setup](#development-setup) · [Tests](#running-the-tests) · [Goldens](#golden-scenarios-and-the-simulation-harness) · [Issues](#writing-an-issue) · [Docs and design rules](#docs-first-and-the-design-rules) · [Changelog and versions](#changelog-version-and-fix-history) · [PR checklist](#pull-request-checklist)

## Branches and where a change goes

Source: [claude.md, Git Workflow](claude.md#git-workflow) and `tools/deploy.py`.

- **`0.9.x` is the long-lived line** for Issue #1230 (the new prediction engine, the operation timeline and the dashboard redesign). Work for it branches from `origin/0.9.x` and its pull request targets `0.9.x`. Versions on this line go 0.9.0 → 0.9.1 → ...
- **`main` carries the 0.8.x fixes.** A fix that is not 0.9 work goes to `main` as the next 0.8.x and reaches `0.9.x` through the merge below.
- **`main` is merged into `0.9.x`, never rebased,** through a pull request so CI runs, before every deploy and whenever `main` has something worth having on the server. On that merge, `const.py` `VERSION` and `manifest.json` keep the branch's 0.9.N, `CHANGELOG.md` keeps both entries newest first, and `fix_history.jsonl` keeps both lines.
- **The deploy guard enforces it.** `tools/deploy.py` refuses to deploy `0.9.x` unless `origin/main` is already merged into it (`--allow-stale-main` overrides). Other branches are not checked.
- **Never deploy from several worktrees.** Each deploy uploads the whole `custom_components/climate_advisor/` directory and overwrites the previous one, so only the last deploy survives. Put every fix that should be on the server into one branch, then deploy once. While the server runs 0.9.x, never deploy from `main`: it would remove the 0.9 work and may meet stored data in a newer format.
- **Deploying** (owner only, it needs `.deploy.env`, see [SSH-SETUP](docs/SSH-SETUP.md)): `python tools/deploy.py` validates, backs up, copies, restarts and checks the logs; `--dry-run`, `--skip-restart` and `--rollback` (restore the newest backup) are the other modes.
- **Names:** create the issue first and use its real number everywhere: branch `fix/<N>-short-description` or `feature/<N>-short-description`, commit and PR title `Fix #<N>: ...` or `Feat #<N>: ...`. Never guess an issue number.

## Development setup

Source: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

1. Install [uv](https://docs.astral.sh/uv/) (Linux, macOS, WSL: `curl -LsSf https://astral.sh/uv/install.sh | sh`; Windows: `python -m pip install --user uv`). uv fetches the Python in `.python-version` (3.14) and the locked dependencies.
2. Run `python scripts/test.py unit` once. It creates the unit tier's own environment (`.venv-test`); the real-HA tier gets a separate one, because the two must never share an environment.
3. After changing dependencies, run `uv lock` and commit `uv.lock`; CI uses `--locked`.

On Windows the real-HA tier runs inside WSL (distro from `CA_WSL_DISTRO`, default `Ubuntu`), because Home Assistant imports the Unix-only `fcntl`.

## Running the tests

| What | Command | Notes |
|---|---|---|
| Unit tier (`tests/`, Home Assistant stubbed) | `python scripts/test.py unit` | Always runs with warnings as errors (`PytestUnraisableExceptionWarning`, `RuntimeWarning`). `CA_UNIT_WORKERS=8` runs it in parallel; prefer 8 to `auto`, which can run out of RAM. Unset or `0` is serial. |
| Real-HA tier (`tests_ha/`, a real Home Assistant core) | `python scripts/test.py ha` | `CA_HA_WORKERS=auto` is fine locally; CI uses `4`. Run it when you touch lifecycle, listeners, timers or persistence. |
| Both tiers | `python scripts/test.py` | Extra arguments go to pytest, e.g. `python scripts/test.py unit tests/test_classifier.py -k hot`. |
| Changed test files in isolation | `python scripts/test_changed_isolated.py --base origin/main` | CI runs it with `--strict` (order-dependence guard, #1186). |
| Dashboard UI specs (`tests/ui/*.spec.js`) | `npm install`, then `npx playwright test` | Served by `tests/ui/mock-server.js` on port 7531. Set `MOCK_PORT` to run several worktrees side by side. |
| Real-browser checks of the dashboards | `<python-with-playwright> tests/browser/check_status_final.py <repo-root> <screenshot-dir>` and `.../check_new_dashboard.py <repo-root> <screenshot-dir>` | Not collected by pytest (CI has no browser). They need a Python environment with the `playwright` package and Chromium (the docstrings call it `pwvenv`). They check 1280 / 390 px in light and dark: console errors, horizontal scroll, contrast, every button. |
| Golden scenarios | `python tools/simulate.py` | See [below](#golden-scenarios-and-the-simulation-harness). `tests/test_production_harness.py` runs them in the unit tier too. |

A skip caused by a missing dependency fails the run. Test-writing rules (async mocks, partial instantiation, "never mirror the logic under test") are in [claude.md, Testing Requirements](claude.md#testing-requirements-critical).

### Lint and validate

CI runs exactly these (`validate.yml`, job `validate-and-test`):

```bash
uv run --locked --only-group test python tools/validate.py
uv run --locked --only-group test ruff check .            # add --fix to fix
uv run --locked --only-group test ruff format --check .   # drop --check to format
```

Ruff settings are in `pyproject.toml` (line length 120). The pre-commit hooks (`.pre-commit-config.yaml`) run check-yaml, check-json, whitespace fixers, ruff `--fix`, ruff-format, `tools/validate.py`, `tools/simulate.py --check-integrity` and, on push, pytest. Ruff can change files during a commit: re-stage them and commit again. Lint before running the tests.

### CI

`validate.yml` (`validate-and-test`, `ha-real`), `hassfest.yml` and `hacs.yml` run on pushes and pull requests to both `main` and `0.9.x`. The required checks on `main` are `validate-and-test`, `ha-real` and `validate` (Hassfest). The nightly (`nightly.yml`) runs generated household days and the full real-HA tier and opens a tracking issue when it fails. Details: [DEVELOPMENT.md, CI](docs/DEVELOPMENT.md#ci).

## Golden scenarios and the simulation harness

Sources: [claude.md, Golden Simulation Test Policy](claude.md#golden-simulation-test-policy), [Sim Harness Brief](docs/sim-harness-brief.md), [Sim Harness Spec](docs/sim-harness-spec.md), `tools/simulate.py --help`.

### One engine

There is no second simulator to keep in sync (Issue #236). `tools/simulate.py` is a report and CLI shell over `tools/sim_harness/`, which drives the **real** production `AutomationEngine` headless with FakeHass and a virtual clock. A scenario asserts against what production actually does: its decisions (the `event_log`), its service calls (the `action_log`) and live engine attributes. A scenario can only assert something production really emits; never invent a decision type.

- **Tier A** is that harness: fast, runs every commit, and `tests/test_production_harness.py` puts every golden through the real engine.
- **Tier B** is the real-Home-Assistant tier (`tests_ha/`, `python scripts/test.py ha`, CI job `ha-real`): the real coordinator inside a real HA core, for state-listener timing, restarts and persistence, timers, unit handling and unload. (HeadlessTarry, a Docker harness for thermostat hardware, is a proposal and is not in the repository.)
- **`track: "integration"`** marks an assertion that needs the coordinator's listener layer, timers or startup lifecycle. It is skipped in bare-engine Tier A runs and evaluated when the scenario sets `"use_coordinator": true` (a real headless coordinator, #474) or in Tier B.
- **`simulator_support: false`** is a legacy tag: the real engine can evaluate these assertions once the scenario supplies the inputs they need (for example a `predicted_indoor` curve for the ceiling guard). On this branch `tools/simulate.py` never skips them.

### Pending, golden, locked

- New scenarios start in `tools/simulations/pending/` (`python tools/simulate.py --pending -v`). After review they move to `golden/`, or to `unsupported/`, which records deliberate scope decisions and is never deleted.
- **Goldens are locked.** Each file's hash is signed into `tools/simulations/golden/MANIFEST.json`; the pre-commit hook runs `--check-integrity` on every commit. Never edit a golden without the owner's explicit approval.
- **Promotion or change needs an owner-approved card.** Show the scenario in plain words (description, each event and its intended outcome, each assertion and why), have the owner confirm it matches real HVAC behaviour, run it, then:

  ```bash
  python tools/simulate.py --sign <name>        # prints the card and updates MANIFEST.json
  python tools/simulate.py --check-integrity    # confirm the manifest is clean
  python tools/simulate.py                      # every golden still passes
  ```

  On Windows run `--fix-crlf` before `--sign`.
- **Review checklist** before writing or changing a golden: realistic HVAC conditions; appropriate comfort (or savings with `aggressive_savings`); assertions grounded in [08-COMPUTATION-REFERENCE](docs/08-COMPUTATION-REFERENCE.md); `reason` fields that explain the HVAC logic; and no hand-set `classification` field (`hvac_mode`, `windows_recommended`, ...) that `classifier.py` would not derive for that day type (Issue #867).

### The three-exercise protocol

Any work that creates, changes or reviews scenarios answers these in the scenario verdict or report before a scenario becomes golden (applied, for example, in `plans/test-automation/02-step2-design.md`):

1. **Scenario effectiveness:** would the scenario fail if the named behaviour were deleted from production?
2. **Docs ↔ production alignment:** does the documentation match what production does? (With one engine, that is the only axis that can drift.)
3. **Occupant outcome:** what does the person at home experience if this test fails to catch a regression?

### Changing the assertion layer

Any change to `tools/sim_harness/outcomes.py` (`production_outcome_at`, `production_temp_at`, `check_assertion`, the event-to-outcome mapping) needs a written semantic justification of why the previous semantics were wrong, not just that the change makes a test pass. A purely additive assertion type must show that no existing result can change. Ask: *could this change let a real future failure pass silently?* Examples: the "Rule-8 justification" paragraphs in [sim-harness-spec](docs/sim-harness-spec.md) and [household-promises-spec](docs/household-promises-spec.md).

## Writing an issue

- **Start with the person at home.** The first sentence says what the occupant experiences ("the AC runs while free outdoor air would have cooled the house"), then the mechanism. The specs' "Occupant first:" paragraphs (for example in [household-promises-spec](docs/household-promises-spec.md)) show the style.
- **Label severity** with one of the repository's labels, chosen by how likely it is to happen, how the household recovers and how long it lasts:

  | Label | Meaning (from the label) |
  |---|---|
  | `severity: critical` | Safety, data loss, or major control failure |
  | `severity: high` | Meaningful comfort, cost or trust impact |
  | `severity: medium` | Narrow or mitigated impact |
  | `severity: low` | Cosmetic or developer-only impact |

- **Check the scope facts first** ([claude.md, Scope Facts](claude.md#scope-facts--check-these-before-calling-anything-a-bug-issue-1197)): fan mode `both` is not supported; a door or window opening never pauses HVAC instantly (each sensor waits out its delay); changes made during the ~5-minute startup window are not tracked by design.
- Every feature or fix has a tracking issue. If it is a sub-issue of an epic, also post a status comment on the epic when a phase ships.
- **Mirror check before closing a fix:** most mechanisms come in pairs (open/close, start/stop). When a root cause is a narrow mechanical pattern, check the opposite path for the same defect and file or fix it; call it out separately, never as a deferred aside ([claude.md, Symmetry/Mirror Check](claude.md#symmetrymirror-check-critical--before-closing-any-bug-fix)).

## Docs first, and the design rules

- **Read the docs before the code.** The docs are tiered: Tier 1 overviews ([00-PROJECT-INSTRUCTIONS](docs/00-PROJECT-INSTRUCTIONS.md), [01-STRATEGY-AND-DESIGN](docs/01-STRATEGY-AND-DESIGN.md), [02-ARCHITECTURE-REFERENCE](docs/02-ARCHITECTURE-REFERENCE.md)), Tier 2 module briefs (`*-brief.md`) and Tier 3 specs (`*-spec.md`). Start at the Anchors table in [00-PROJECT-INSTRUCTIONS](docs/00-PROJECT-INSTRUCTIONS.md); the Architecture Reference is the second index. For a runtime problem, look at the logs and recorded data before forming a hypothesis ([claude.md, Investigation Protocol](claude.md#investigation-protocol)).
- **Keep docs current.** A doc you touch gets its Anchors table updated; new modules get a Tier 2 brief and a Tier 3 spec, from the templates in [docs/templates](docs/templates/). User-facing changes also update [README.md](README.md).
- **Home Assistant Boundary Rule** ([claude.md](claude.md#home-assistant-boundary-rule-critical)): Climate Advisor must not modify, write to or interact with anything in Home Assistant outside its own scope. Allowed: the integration's own files; `climate` calls (`set_hvac_mode`, `set_temperature`, `set_fan_mode`) to the configured thermostat; the configured `notify` service and `notify.send_email`; `turn_on`/`turn_off` on the configured whole-house fan entity (`fan.*` or `switch.*`); read-only `weather.get_forecasts` on the configured weather entity; and, in the config root, the per-zone data files listed in `const.py` `ENTRY_SCOPED_FILES` (the single source of truth) plus `climate_advisor_investigation_reports.json`. Out of scope: other integrations or their config, `configuration.yaml` and other HA config files, unrelated services (for example `homeassistant.restart`), files elsewhere, add-on configuration, and deploying outside `/config/custom_components/climate_advisor/`. If a change would cross the line, stop, flag it as a **boundary violation**, explain it and ask the owner; an approved exception is logged in [HA-BOUNDARY-EXCEPTIONS](docs/HA-BOUNDARY-EXCEPTIONS.md) with date, reason, risk and a resolution plan.
- **Other standing rules** in [claude.md](claude.md): keep the modular architecture (one responsibility per module); every new behaviour gets logging, a place on the status page and chart coverage; blocking I/O stays off the event loop; config fields follow the Config UI Field Standard (`strings.json` and `translations/en.json` byte-identical); the briefing stays friendly and explains why.

## Changelog, version and fix history

Source: [claude.md, Release Process](claude.md#release-process-critical--must-follow-this-order), `tools/add_fix_entry.py --help`.

- **Every PR bumps the version** in both `custom_components/climate_advisor/const.py` (`VERSION`) and `manifest.json` (`"version"`). `tests/test_version_sync.py` checks they match. If two open PRs claim the same number, the one that merges second takes the next one.
- **`CHANGELOG.md`** follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/): a new `## [X.Y.Z] — <date>` section, newest on top, with plain-English bullets about what the occupant now experiences (`Fix #N: ...`, `Feat #N: ...`).
- **`fix_history.jsonl`** gets one record per fixed or added issue, written only with the tool (a hand-edited bad line breaks the parser for every later record):

  ```bash
  python tools/add_fix_entry.py --issue N --version X.Y.Z --title "..." --scope "..." [--user-summary "Fix #N: ..."]
  ```

  `--title` and `--scope` are developer detail. `--user-summary` is the occupant-facing outcome, only when there is one; never invent it. `--allow-reopened` permits a second entry for a reopened issue that got a materially different fix. Search first with `python tools/fix_search.py "<topic>"`.
- **Comment on the issue.** Each fix PR also gets a plain-English comment on its issue: what was fixed, the PR link, and what the household sees before versus after.
- GitHub Releases are tagged only after the version bump is merged (draft first).

## Working with an AI assistant

[claude.md](claude.md) and [agents.md](agents.md) are the instructions for AI assistants on this repository. Two rules affect everyone: the human reviews and commits the changes, and nothing posted to GitHub (commits, PR descriptions, PR or issue comments) carries an AI attribution line.

## Pull request checklist

- [ ] There is an issue, and its number is in the branch, the commit and the PR title (`Fix #N:` / `Feat #N:`).
- [ ] The PR targets the right branch: `0.9.x` for #1230 work, `main` for 0.8.x fixes.
- [ ] The description starts with what the person at home gets.
- [ ] `tools/validate.py`, `ruff check .` and `ruff format --check .` are clean.
- [ ] `python scripts/test.py unit` passes; `python scripts/test.py ha` too if you touched lifecycle, listeners, timers or persistence.
- [ ] Dashboard changes: `npx playwright test` and the two `tests/browser/` checks pass.
- [ ] Behaviour changes: `python tools/simulate.py` and `--check-integrity` pass; new or changed goldens have an owner-approved card, the three exercises answered and a `--sign`; no locked golden changed without the owner's OK.
- [ ] Any `outcomes.py` change carries its semantic justification.
- [ ] Mirror path checked; any finding filed or fixed and named on its own.
- [ ] Version bumped in `const.py` and `manifest.json`; `CHANGELOG.md` entry; `fix_history.jsonl` entry via `tools/add_fix_entry.py`.
- [ ] Docs touched have current Anchors tables; the README is updated for user-facing changes.
- [ ] No boundary violation (or an approved exception is logged).
- [ ] After merge: a plain-English comment on the issue with the fix, the PR link and before vs after.
