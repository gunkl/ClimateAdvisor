<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) -->

# Home Assistant Boundary Exceptions

This file tracks approved exceptions where Climate Advisor interacts with Home Assistant outside its own integration directory (`custom_components/climate_advisor/`). Each exception should be periodically reviewed and resolved when possible.

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| What is the one approved exception to the HA boundary rule and why? | Climate Advisor writes `climate_advisor_learning.json` to the HA config root. This is the standard persistent storage location for custom integrations; the file is owned entirely by CA and deleting it resets learning gracefully. | [§1. Learning Engine Database File](HA-BOUNDARY-EXCEPTIONS.md#1-learning-engine-database-file) |
| What is the resolution plan for the learning DB file exception? | Not urgent — current file-based persistence is already atomic/race-safe (Issue #493). Migration to HA's `Store` API is tracked as a future evaluation, not a fix for a known defect. See [Issue #779](https://github.com/gunkl/ClimateAdvisor/issues/779). | [§1. Learning Engine Database File](HA-BOUNDARY-EXCEPTIONS.md#1-learning-engine-database-file) |
| How often should active exceptions be reviewed? | Quarterly or before each minor version release. For each exception: is it still necessary, has HA added a better-supported alternative, can it move inside the integration's scope, what is the current risk level? | [§Review Schedule](HA-BOUNDARY-EXCEPTIONS.md#review-schedule) |
| What is the dev-only sim integrations exception? | Two never-shipped test integrations (`ca_dev_thermostat_sim`, `ca_dev_weather_proxy`, Issue #809) were installed into `config/custom_components/` on the user's live HA instance via SSH, at explicit user request, for manual multi-zone testing. | [§2. Dev-Only Test Integrations](HA-BOUNDARY-EXCEPTIONS.md#2-dev-only-test-integrations) |

## Active Exceptions

### 1. Learning Engine Database File

- **Date**: 2026-03-18
- **What**: The learning engine writes `climate_advisor_learning.json` to the HA config root (`/config/climate_advisor_learning.json`)
- **Why**: HA's config directory is the standard persistent storage location for custom integrations that need to store state across restarts. The learning engine needs a 90-day rolling window of daily observations and user feedback to generate suggestions.
- **Risk**: Low. The file is a single JSON file owned entirely by Climate Advisor. It does not modify any existing HA files. If the file is deleted, the learning engine resets gracefully with no impact to HA.
- **Resolution plan**: A brief investigation (2026-08-29) found the current persistence already atomic/race-safe (`tempfile.mkstemp()` + `os.replace()`, Issue #493) — there is no known defect driving a migration. Migrating to HA's `Store` API would match HA's recommended pattern but requires a careful one-time data-migration path for existing installs and an equivalent read path for `tools/learning_db.py`/`tools/thermal_health.py` (which currently SSH-read the file directly). Tracked as a future evaluation in [Issue #779](https://github.com/gunkl/ClimateAdvisor/issues/779), not targeted to a specific version.
- **Status**: Active — not urgent; see Issue #779 for the migration tradeoffs

### 2. Dev-Only Test Integrations

- **Date**: 2026-09-01
- **What**: Two never-shipped custom integrations (`ca_dev_thermostat_sim`, `ca_dev_weather_proxy`, built for Issue #809, source at `dev_tools/ha_test_integrations/` in this repo) were copied via SSH into `/config/custom_components/ca_dev_thermostat_sim/` and `/config/custom_components/ca_dev_weather_proxy/` on the live HA instance, outside `custom_components/climate_advisor/`.
- **Why**: The user needed a second, genuinely independent climate + weather entity to manually test multi-zone support (#796/#808) end-to-end, and has only one real thermostat/weather integration configured. Explicitly requested by the user after being flagged as a boundary consideration (AskUserQuestion, confirmed).
- **Risk**: Low-medium. Neither integration is deployed by `tools/deploy.py` (confirmed out of its scope) or touched by any Climate Advisor code. They add two new `climate.*`/`weather.*` entities and their own config entries — reversible by removing the integrations via Settings → Devices & Services and deleting the two folders. No interaction with any existing HA config, entity, or integration.
- **Resolution plan**: Not applicable — these are intentionally temporary dev/test tooling, not a production dependency. Remove both integrations (UI removal + folder deletion) once manual multi-zone testing is complete; no code change required in Climate Advisor itself.
- **Status**: Active — installed for ongoing multi-zone manual testing; safe to remove any time

---

## Resolved Exceptions

_None yet._

---

## Review Schedule

Review all active exceptions quarterly or before each minor version release. For each exception, ask:
1. Is this still necessary?
2. Has HA added a better-supported way to accomplish this?
3. Can this be moved inside the integration's own scope?
4. What is the current risk level?
