# HACS Compliance Brief

**Anchor:** `hacs-compliance` | **Tier:** 2 | **Status:** Current as of 2026-07-02

This document captures all requirements for ClimateAdvisor to remain compliant with HACS
(Home Assistant Community Store) and for the default repository PR (#8117) to merge smoothly.

## Integration Type

`manifest.json` must contain `"integration_type": "helper"`.

**Why `helper`:**
- HA docs define `helper` as "provides an entity to help the user with automations like input
  boolean, derivative or group"
- Climate Advisor assists users with HVAC automation — it is an automation helper that sits above
  the thermostat and climate entities rather than a hardware hub or cloud service in its own right.

**Rule:** Never change `integration_type` away from `"helper"` without updating this doc.

## manifest.json Required Fields

All fields below must be present and valid. The HACS automated validator checks them before
any human reviewer sees the PR.

| Field | Required | CA value | Notes |
|---|---|---|---|
| `domain` | Yes | `climate_advisor` | Must match directory name; `[a-z0-9_]` only |
| `name` | Yes | `Climate Advisor` | Display name in HACS store |
| `codeowners` | Yes | `["@gunkl"]` | GitHub usernames of maintainers |
| `config_flow` | Yes | `true` | All CA config is via config flow |
| `dependencies` | Yes | `["weather", "climate", "http"]` | Required HA integrations |
| `documentation` | Yes | GitHub repo URL | Must resolve to a real page |
| `integration_type` | Yes | `"helper"` | See section above — do not change |
| `iot_class` | Yes | `"local_polling"` | CA polls local HA entities |
| `issue_tracker` | Yes | GitHub issues URL | Must resolve to real issues page |
| `requirements` | Yes | `["anthropic>=0.49.0"]` | pip packages |
| `version` | Yes | current semver | Must match `const.py VERSION` |

## hacs.json Required Fields

Minimum required: `name` field. CA's `hacs.json` also sets `render_readme`,
`homeassistant` (minimum HA version), and `hide_default_branch`.

```json
{
  "name": "Climate Advisor",
  "render_readme": true,
  "homeassistant": "2024.6.0",
  "hide_default_branch": true
}
```

**Rule:** `render_readme: true` means HACS renders `README.md` as the store page. The README
must always look good and show accurate version information.

## brand/icon.png

`brand/icon.png` at the repo root is required by HACS (not required by HA core). It provides
the integration's icon in the HACS store browser. **Never delete this file.**

## GitHub Releases

HACS requires at least one formal **GitHub Release** (not just a git tag). Tags alone are not
sufficient — HACS reads from the GitHub Releases API.

**Rule:** Every version bump that goes to production must have a corresponding GitHub Release
created via `gh release create` or the GitHub UI.

## README Version Badge

The README version display is a **dynamic shields.io badge** — do not replace it with a
hardcoded string:

```markdown
[![Latest Release](https://img.shields.io/github/v/release/gunkl/ClimateAdvisor?label=version&style=flat-square)](https://github.com/gunkl/ClimateAdvisor/releases/latest)
```

This reads the GitHub Releases API at render time and always reflects the latest published
release without any manual updates. Hardcoded version strings drift (CA was at v0.4.28 in the
README when the integration was at v0.4.51 — an 18-version gap).

## GitHub Actions (CI)

Two workflows must pass before HACS review:

1. **HACS Action** — validates HACS compatibility (manifest, hacs.json, brand assets, releases)
2. **Hassfest** — validates HA integration manifest correctness

Both must be green **on a run triggered after the latest release was created**. Old action runs
don't count — the PR checklist requires links to runs that reflect the current state.

## HACS PR Submission Checklist (6 items enforced by bot)

- [ ] Read the publishing documentation at hacs.xyz/docs/publish/start
- [ ] Added the HACS action to the repository
- [ ] Added the hassfest action to the repository (integrations only)
- [ ] Both actions passing without any disabled checks
- [ ] Added link to action run in PR description
- [ ] Created a new release after validation actions ran successfully

**Bot enforcement:** Missing any item causes automatic PR rejection. The PR must be closed
and re-submitted (editing a bot-rejected PR does not work).

## Review Process

**HACS review is almost entirely automated.** Human reviewer Frenck only verifies the checklist
and CI, then merges. Of the last 10+ merged integration PRs, Frenck's comment was always:
> "Hi @username, thanks for the submission! 👋 Approving and merging now. ../Frenck"

PRs are processed FIFO — the queue has 60+ pending integrations (as of 2026-07-02). Typical
wait: 6–10 weeks depending on queue depth.

## Merge Conflict Strategy

The most common reason clean HACS PRs get stuck: alphabetical insert-point collision in
`integrations.json`. CA's entry is under `c` (climate_advisor). If another `c*` integration
lands while our PR is open:

1. Frenck marks the branch "out of date" (or bot does)
2. Rebase the PR branch on latest hacs/default main: `git fetch hacs && git rebase hacs/default/main`
3. Push the rebased branch — CI re-runs automatically
4. Frenck merges on next pass

**No code changes needed** — it's just a rebase to resolve the alphabetical position conflict.

## Ongoing Maintenance Rules

1. `integration_type: "service"` must remain in `manifest.json` — do not remove
2. `brand/icon.png` must remain at repo root — do not delete
3. README version is a dynamic badge — do not replace with a hardcoded string
4. Every production version bump must have a GitHub Release (not just a git tag)
5. Re-run HACS action + hassfest before updating the PR description links
6. If hacs/default PR shows "out of date": rebase, don't merge

## What HACS Does NOT Require (Common Misconceptions)

- `quality_scale` in manifest — optional; adds quality signal but not validated by HACS
- `info.md` — deprecated; HACS now uses README (with `render_readme: true`)
- Separate branch for HACS — CA's main branch is fine
- `homekit` or `zeroconf` — optional discovery hints, not required
