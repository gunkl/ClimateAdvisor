# Contributing to Climate Advisor

Climate Advisor runs real heating, cooling and fans in someone's home. Every change is judged by what the person living there experiences: comfort, cost and trust that the house does what the briefing says. This guide collects the project's existing rules in one place. Where a rule is written down in more detail elsewhere, the section links to it; that document stays the authority.

Contents: [Issue first](#start-with-an-issue) · [Evidence or hypothesis](#evidence-or-hypothesis-say-which) · [How we fix](#how-we-diagnose-fix-and-verify) · [Automatic check](#the-automatic-submission-check) · [Branches](#branches-and-where-a-change-goes) · [Setup](#development-setup) · [Tests](#running-the-tests) · [Goldens](#golden-scenarios-and-the-simulation-harness) · [Issues](#writing-an-issue) · [Docs and design rules](#docs-first-and-the-design-rules) · [Changelog and versions](#changelog-version-and-fix-history) · [PR checklist](#pull-request-checklist)

## Start with an issue

**Every pull request links an issue.** What has to happen before the PR depends on the kind of change:

| Change | Before you open a PR |
|---|---|
| **Bug fix** | Open the bug issue with its evidence, or a labelled hypothesis ([below](#evidence-or-hypothesis-say-which)). You can then open the PR straight away: it's a **proposed fix**, reviewed together with the issue. The analysis in the PR template is still required, and the data in the issue counts for more than the fix. |
| **Feature or enhancement** | Open the issue and **agree the approach first**. Wait until the issue has the `approach agreed` label. A feature PR without it is asked to settle the approach in the issue first. |
| **Typo, broken link, translation fix** | No issue needed. |

In this project the fix is usually the easy part. Most of the work is finding out what the house actually did, why, and what else the same cause touches. The issue and the PR template exist for that work, and [How we diagnose, fix and verify](#how-we-diagnose-fix-and-verify) describes it.

## Evidence or hypothesis: say which

Every bug report is one of two kinds, and the issue form asks which.

**Evidence-backed:** you saw it happen, and you attach what shows it. Data beats a proposed fix: a report with diagnostics and logs and no fix is more useful than a fix with no data.

- Diagnostics for the zone: **Settings → Devices & Services → Climate Advisor → ⋮ → Download diagnostics**, or the dashboard's Debug tab.
- Logs covering the time it happened (Debug tab → **Download logs**). For more detail, turn on **Enable debug logging**, reproduce the problem, then turn it off.
- When it happened (date, time and time zone), what you expected, what the house did, and anything you did yourself (changed the setpoint, opened a window, switched the fan).
- Climate Advisor and Home Assistant versions, the thermostat and fan hardware, and the fan mode.
- If you have them: a screenshot of the dashboard chart, the AI Investigator report, or the household-promise misses on the Analysis tab.
- A reproduction in the [simulation harness](#golden-scenarios-and-the-simulation-harness) also counts as evidence: a generated day (`python tools/simulate.py --generate-day N`) or a scenario in `tools/simulations/pending/` that shows the behaviour through the real engine.

**Hypothesis:** you think something is wrong, but have no data from a real house or the harness that shows it. Reading the code and spotting a likely bug is a hypothesis. It's welcome, but label it as one (the form's "Hypothesis" choice sets the `hypothesis` label) and include:

- what you think happens, and what the person at home would experience;
- where you read it (the doc section, file and function);
- what data would confirm it or rule it out.

Don't present a hypothesis as a finding. Many things that look like bugs from the code are deliberate decisions: check the [scope facts](claude.md#scope-facts--check-these-before-calling-anything-a-bug-issue-1197) and the closed `wontfix` issues.

## How we diagnose, fix and verify

Every fix in this project follows this method, whether a person or an AI assistant does the work. The [pull request template](.github/pull_request_template.md) asks you to show each step.

1. **Occupant first.** Start with what the person living in the home experiences ("the AC runs while outdoor air would have cooled the house for free"), then the mechanism. A finding that changes nothing for the occupant (duplicate commands, log noise) isn't a bug here.
2. **Evidence before hypothesis, docs before code.** Look at the logs and recorded data first. Then read the relevant docs: start at the Anchors table in [00-PROJECT-INSTRUCTIONS](docs/00-PROJECT-INSTRUCTIONS.md), then the module's brief and spec. Open source files last, to test a specific hypothesis.
3. **Check it isn't already decided.** Read the [scope facts](claude.md#scope-facts--check-these-before-calling-anything-a-bug-issue-1197). Search open and closed issues (including `wontfix`), `CHANGELOG.md`, and `python tools/fix_search.py "<topic>"`.
4. **Find where it came from.** Run `git log -p -S "<key fragment>" -- <file>`. Then say which it is:
   - a **regression**: name the commit or PR, and the test that would have caught it;
   - an **original design gap**: it has always worked this way.
5. **Map the states, not just before and after.** Make a table of the conditions involved, for example mode × fan × window × automation on/off × in grace. Include edge cases, intermediate states and controls, and say what happens in each, today and with the fix.
6. **Measure the blast radius across the whole repository.** Grep each changed symbol across `custom_components/climate_advisor/`, `tools/`, `tests/`, `tests_ha/` and `docs/`, not just the files you think are related, and list every writer and every reader. A search limited to the obvious files reliably undercounts.
7. **Mirror check.** Most mechanisms come in pairs (start/stop, open/close, enter/exit). When the root cause is a narrow pattern (an unchecked result, an unguarded flag), check the opposite path for the same defect. Report what you find as its own item: fix it in the same PR if it's small, otherwise file it ([claude.md, Symmetry/Mirror Check](claude.md#symmetrymirror-check-critical--before-closing-any-bug-fix)).
8. **Fix at the right layer, for every household.** Fix the cause, not the symptom: a wrong check gets a fix to the check, not to the engine. Nothing should be tuned to one house. Keep the [design rules](#docs-first-and-the-design-rules): the boundary rule, one responsibility per module, logging and status coverage, translations.
9. **Prove the test would catch it.** A new test must fail with the fix reverted and pass with it, and the PR must say you checked. Controls (cases the fix must not change) must pass for the right reason, not because some older code path happens to cover them. Then:
   - run the unit tier;
   - run the real-HA tier if you touched lifecycle, listeners, timers or persistence;
   - for behaviour changes, run `python tools/simulate.py` and generated days for the area;
   - for scenario work, answer the [three exercises](#the-three-exercise-protocol).
10. **Keep the docs true.** If production now behaves differently from a spec or brief, update that doc in the same PR.

## The automatic submission check

A workflow (`.github/workflows/submission-check.yml`) reads every new issue and pull request and posts one comment. The comment either lists what's missing and how to complete it, or says the submission is complete. It helps you finish a submission; it never closes or blocks anything.

- **When it runs:** at most twice per submission. The first time is when you open it. The second is after your first revision (an edit, a push or a reply). Each time it waits 5 minutes for further edits first. After that it stays quiet; the maintainer can comment `/recheck` to run it again.
- **Labels:**
  - `needs-info` while something is missing;
  - `submission: complete` once nothing is missing;
  - `hypothesis` when the issue form says so.
- **What it checks:**
  - **A script:**
    - the form fields are filled in;
    - the issue says whether it's evidence or a hypothesis;
    - the PR links an issue, and for a feature that issue has `approach agreed`;
    - the PR template sections are filled in;
    - the version, changelog and fix-history entries are present.
  - **An AI review (read-only):**
    - does the submission start with the person at home;
    - is a hypothesis presented as a finding;
    - do the analysis sections say something real;
    - does it match a closed `wontfix` issue, or duplicate another issue?
- **Limits:** the AI review runs for at most 10 submissions a day across the project. Each contributor gets at most 3 issues and 3 PRs a day, and 30 of each a month. Past a limit, the script checks still run, and the comment says the AI review is paused.
- **Skipped:** the maintainer's own submissions, Dependabot, and the nightly-failure issues.

## Branches and where a change goes

Source: [claude.md, Git Workflow](claude.md#git-workflow) and `tools/deploy.py`.

- **`main` is the current line** (0.9 and later). Work branches from `origin/main` and its pull request targets `main`. The 0.9 line was built on a long-lived `0.9.x` branch and promoted to `main` at 0.9.14; `0.9.x` is retired.
- **`0.8.x` is the 0.8 maintenance line,** cut from `main` at 0.8.72. A fix for 0.8 users branches from `origin/0.8.x`, targets `0.8.x` and is released as the next 0.8.N on that branch. If the bug also exists on `main`, cherry-pick the fix into `main` in its own pull request. The two lines are never merged into each other.
- **Deploy only from `main`.** `tools/deploy.py` refuses to deploy `0.8.x`, because it would roll the server back to 0.8 (`--allow-maintenance-branch` overrides).
- **Never deploy from several worktrees.** Each deploy uploads the whole `custom_components/climate_advisor/` directory and overwrites the previous one, so only the last deploy survives. Put every fix that should be on the server into one branch, then deploy once.
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
| Real-browser checks of the dashboards | `<python-with-playwright> tests/browser/check_status_final.py <repo-root> <screenshot-dir>` and `.../check_new_dashboard.py <repo-root> <screenshot-dir>` | Not collected by pytest (CI has no browser). They need a Python environment with the `playwright` package and Chromium (the docstrings call it `pwvenv`). They check 1280 / 390 px in light and dark: console errors, horizontal scroll, contrast, every button. `tests/browser/check_i18n_rtl.py <repo-root> <screenshot-dir>` (same environment) opens Status, Settings, Analysis and the accuracy page in Arabic (right-to-left, mirrored layout) and English at both widths and both themes: console errors, sideways scroll, boxes that spill, text outside its card, and the screenshots. |
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

`validate.yml` (`validate-and-test`, `ha-real`), `hassfest.yml` and `hacs.yml` run on pushes and pull requests to both `main` and `0.8.x`. The required checks on `main` are `validate-and-test`, `ha-real` and `validate` (Hassfest). The nightly (`nightly.yml`) runs generated household days and the full real-HA tier and files an issue when it fails; `nightly-autofix.yml` then has Claude diagnose it and, when it can show a fix, open a draft PR for review. Details: [DEVELOPMENT.md, CI](docs/DEVELOPMENT.md#ci).

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

- **Use the issue forms.** The bug form asks for what the person at home experienced, the evidence or hypothesis, the times, versions and hardware ([Evidence or hypothesis](#evidence-or-hypothesis-say-which)). The first sentence says what the occupant experiences, then the mechanism.
- **Label severity** with one of the repository's labels, chosen by how likely it is to happen, how the household recovers and how long it lasts:

  | Label | Meaning (from the label) |
  |---|---|
  | `severity: critical` | Safety, data loss, or major control failure |
  | `severity: high` | Meaningful comfort, cost or trust impact |
  | `severity: medium` | Narrow or mitigated impact |
  | `severity: low` | Cosmetic or developer-only impact |

- **Check the scope facts first** ([claude.md, Scope Facts](claude.md#scope-facts--check-these-before-calling-anything-a-bug-issue-1197)): fan mode `both` is not supported; a door or window opening never pauses HVAC instantly (each sensor waits out its delay); changes made during the ~5-minute startup window are not tracked by design.
- Every feature or fix has a tracking issue. If it is a sub-issue of an epic, also post a status comment on the epic when a phase ships.

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

### Instructions for your AI assistant

If an AI assistant writes or reviews your change, give it the instructions below. agents.md and claude.md hold the full rules. Parts of claude.md describe the maintainer's own tools, which you can't use: SSH to the live house, `tools/deploy.py`, `tools/ha_logs.py` and the live API. Use your own install's diagnostics and logs instead.

```text
You are working on Climate Advisor, a Home Assistant integration that controls real heating,
cooling and fans in people's homes. Follow CONTRIBUTING.md, "How we diagnose, fix and verify".

- Every change needs a GitHub issue first. For a bug, write the issue with its evidence
  (diagnostics, logs, times) or a clearly labelled hypothesis; a fix PR may then follow as a
  proposal. For a feature or enhancement, write no code until the issue has the
  `approach agreed` label.
- Start every finding with what the person at home experiences.
- Look at logs and data before forming a hypothesis. Read docs/00-PROJECT-INSTRUCTIONS.md
  (Anchors table) and the relevant brief and spec before opening source files.
- Check the scope facts in claude.md, closed wontfix issues and CHANGELOG.md before calling
  something a bug.
- Find the origin with `git log -S`: a regression (name the commit) or an original design gap.
- Build a state matrix of every relevant condition, including edge cases and controls.
- Grep every changed symbol across the whole repository, and list every writer and reader.
- Check the mirror path (start/stop, open/close, enter/exit) for the same defect, and report it
  as its own item.
- Every new test must fail with the fix reverted. Say so in the PR.
- Never edit a golden scenario or tools/sim_harness/outcomes.py without the maintainer's
  approval and a written justification.
- Add no AI attribution lines to commits, PRs or comments.
- Fill in every section of the pull request template.
```

## Pull request checklist

- [ ] There is an issue (for a feature or enhancement, labelled `approach agreed`), and its number is in the branch, the commit and the PR title (`Fix #N:` / `Feat #N:`).
- [ ] The description fills in every section of the [PR template](.github/pull_request_template.md): evidence, origin, states, blast radius, mirror check, verification.
- [ ] New tests fail with the fix reverted; controls pass for the right reason.
- [ ] The PR targets the right branch: `main` for current work, `0.8.x` only for a 0.8 maintenance fix.
- [ ] The description starts with what the person at home gets.
- [ ] `tools/validate.py`, `ruff check .` and `ruff format --check .` are clean.
- [ ] `python scripts/test.py unit` passes; `python scripts/test.py ha` too if you touched lifecycle, listeners, timers or persistence.
- [ ] Dashboard changes: `npx playwright test` and the two `tests/browser/` checks pass.
- [ ] Behaviour changes: `python tools/simulate.py` and `--check-integrity` pass; new or changed goldens have an owner-approved card, the three exercises answered and a `--sign`; no locked golden changed without the owner's OK.
- [ ] Any `outcomes.py` change carries its semantic justification.
- [ ] Mirror path checked; any finding filed or fixed and named on its own.
- [ ] Version bumped in `const.py` and `manifest.json`; `CHANGELOG.md` entry; `fix_history.jsonl` entry via `tools/add_fix_entry.py`.
- [ ] Translations: every new occupant-facing string is in all 17 languages (`python tools/i18n.py lock` then `check`), no hard-coded English at a sink (see `docs/i18n-spec.md`).
- [ ] Docs touched have current Anchors tables; the README is updated for user-facing changes.
- [ ] No boundary violation (or an approved exception is logged).
- [ ] After merge: a plain-English comment on the issue with the fix, the PR link and before vs after.
