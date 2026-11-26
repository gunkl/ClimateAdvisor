<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) -->

# Home Assistant Boundary Exceptions

This file tracks approved exceptions where Climate Advisor interacts with Home Assistant outside its own integration directory (`custom_components/climate_advisor/`). Each exception should be periodically reviewed and resolved when possible.

The standing allowed scope is in [claude.md, Home Assistant Boundary Rule](../claude.md#home-assistant-boundary-rule-critical) (Issue #1401). In short: `climate.set_hvac_mode` / `set_temperature` / `set_fan_mode` on the configured thermostat; the configured `notify` service and `notify.send_email`; `turn_on` / `turn_off` on the configured whole-house fan entity (`fan.*` or `switch.*`); read-only `weather.get_forecasts` on the configured weather entity; `ai_task.generate_data` (Python helper `ai_task.async_generate_data`) on the chosen or preferred AI Task entity, with a read-only look at that entity's registry entry and subentry options (§3, Issue #1336); the Anthropic cloud API from the deprecated built-in Claude client, until it is removed; and, in the HA config root, the per-zone files listed in `const.py` `ENTRY_SCOPED_FILES` (the single source of truth; the AI investigation history is per zone since Issue #1415) and the temp files of their atomic writes. The services need no entry here; the config-root files keep §1 below for their risk and resolution history. Short-lived worker processes of the same Python interpreter for long pure-Python jobs are the exception in §4 (Issue #1722). Anything else outside the integration directory needs a new entry, approved by the owner.

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| Which files may Climate Advisor write in the HA config root? | The per-zone files whose base names are in `const.py` `ENTRY_SCOPED_FILES` (state, chart log, learning DB, forecast archive, v4 archive/backtest/model, AI investigation history; each `<stem>_<entry_id>.json`, deleted with the zone). The old shared `climate_advisor_investigation_reports.json` is split into every zone once and removed (Issue #1415). `ENTRY_SCOPED_FILES` is the single source of truth; `tests/test_zone_removal_cleanup.py` fails if a per-zone file is missing from it. | [§1. Config-Root Data Files](HA-BOUNDARY-EXCEPTIONS.md#1-config-root-data-files) |
| Which HA services may Climate Advisor call? | `climate` (`set_hvac_mode`, `set_temperature`, `set_fan_mode`) on the configured thermostat, `notify` (configured service and `send_email`), `turn_on`/`turn_off` on the configured fan entity (`fan.*` or `switch.*`), read-only `weather.get_forecasts`, and `ai_task.generate_data` (through `ai_task.async_generate_data`) on the AI Task entity the owner picked, or HA's preferred AI Task entity when none is picked; nothing else in `ai_task`. Read-only: that entity's entity-registry entry and its config subentry options, for the limit check (owner-approved 2026-11-20, Issue #1336). The deprecated built-in Claude client still calls the Anthropic cloud API directly until it is removed. Nothing else. | [claude.md, Allowed scope](../claude.md#home-assistant-boundary-rule-critical), [§3. AI Task](#3-ai-task-generate-data-and-the-limit-lookup-issue-1336) |
| What is the resolution plan for the config-root data files? | Not urgent — current file-based persistence is already atomic/race-safe (Issue #493). Migration to HA's `Store` API is tracked as a future evaluation, not a fix for a known defect. See [Issue #779](https://github.com/gunkl/ClimateAdvisor/issues/779). | [§1. Config-Root Data Files](HA-BOUNDARY-EXCEPTIONS.md#1-config-root-data-files) |
| May Climate Advisor start a process (Issue #1722)? | Yes, one narrow kind, owner-approved 2026-11-26: a short-lived worker of the same interpreter (`sys.executable -I`) that runs an allowlisted function from Climate Advisor's own package. Stdin/stdout pipes only; no files, no network, no HA calls; nice +10; killed on timeout, unload or HA stop; falls back to a thread if it cannot start. Nothing else may start a process. | [§4. Worker Processes](HA-BOUNDARY-EXCEPTIONS.md#4-worker-processes-for-long-pure-python-jobs-issue-1722), [Process Offload Brief](process-offload-brief.md) |
| How often should active exceptions be reviewed? | Quarterly or before each minor version release. For each exception: is it still necessary, has HA added a better-supported alternative, can it move inside the integration's scope, what is the current risk level? | [§Review Schedule](HA-BOUNDARY-EXCEPTIONS.md#review-schedule) |
| What is the dev-only sim integrations exception? | Two never-shipped test integrations (`ca_dev_thermostat_sim`, `ca_dev_weather_proxy`, Issue #809) were installed into `config/custom_components/` on the user's live HA instance via SSH, at explicit user request, for manual multi-zone testing. | [§2. Dev-Only Test Integrations](HA-BOUNDARY-EXCEPTIONS.md#2-dev-only-test-integrations) |

## Active Exceptions

### 1. Config-Root Data Files

- **Date**: 2026-03-18 (learning DB); widened to the full file set 2026-11-07 (Issue #1401: policy text caught up with files the integration already wrote, no code change)
- **What**: Climate Advisor writes these files to the HA config root (`/config/`), and no others:
  - every per-zone file whose base name is listed in `const.py` `ENTRY_SCOPED_FILES` — today `climate_advisor_state.json`, `climate_advisor_chart_log.json`, `climate_advisor_learning.json`, `climate_advisor_forecast_archive.json`, `climate_advisor_v4_archive.json`, `climate_advisor_v4_backtest.json`, `climate_advisor_v4_model.json`, `climate_advisor_investigation_reports.json`. Each is stored per zone as `<stem>_<entry_id>.json` (`storage_paths.resolve_entry_scoped_path`; a legacy unscoped copy is migrated once) and is deleted when its zone is deleted (`async_remove_entry` → `remove_entry_scoped_files`). The list in `const.py` is authoritative; this bullet is a snapshot. The v4 backtest file is read-only to the integration: `tools/predict_backtest.py --publish` writes it from a dev machine.
  - the pre-0.9.6 shared `climate_advisor_investigation_reports.json` (`INVESTIGATION_REPORTS_FILE`), read once at startup: each zone got its own copy (the entries carry no zone tag) and the shared file is removed (Issue #1415: one zone's save erased the other zones' reports).
  - the short-lived temp files of the atomic writes above (`tempfile.mkstemp()` or `<file>.tmp`, then `os.replace()`).
- **Why**: HA's config directory is the standard persistent storage location for custom integrations that need to store state across restarts (state, learning observations, chart history, forecast and prediction archives, the fitted v4 model).
- **Risk**: Low. Every file is JSON owned entirely by Climate Advisor. None modifies an existing HA file. Deleting one resets that piece gracefully with no impact to HA. A new per-zone file must be added to `ENTRY_SCOPED_FILES` (`tests/test_zone_removal_cleanup.py` enforces it); any other new config-root file needs a new entry here.
- **Resolution plan**: A brief investigation (2026-08-29) found the current persistence already atomic/race-safe (`tempfile.mkstemp()` + `os.replace()`, Issue #493) — there is no known defect driving a migration. Migrating to HA's `Store` API would match HA's recommended pattern but requires a careful one-time data-migration path for existing installs and an equivalent read path for `tools/learning_db.py`/`tools/thermal_health.py` (which currently SSH-read the file directly). Tracked as a future evaluation in [Issue #779](https://github.com/gunkl/ClimateAdvisor/issues/779), not targeted to a specific version.
- **Status**: Active — not urgent; see Issue #779 for the migration tradeoffs

### 2. Dev-Only Test Integrations

- **Date**: 2026-09-01
- **What**: Two never-shipped custom integrations (`ca_dev_thermostat_sim`, `ca_dev_weather_proxy`, built for Issue #809, source at `dev_tools/ha_test_integrations/` in this repo) were copied via SSH into `/config/custom_components/ca_dev_thermostat_sim/` and `/config/custom_components/ca_dev_weather_proxy/` on the live HA instance, outside `custom_components/climate_advisor/`.
- **Why**: The user needed a second, genuinely independent climate + weather entity to manually test multi-zone support (#796/#808) end-to-end, and has only one real thermostat/weather integration configured. Explicitly requested by the user after being flagged as a boundary consideration (AskUserQuestion, confirmed).
- **Risk**: Low-medium. Neither integration is deployed by `tools/deploy.py` (confirmed out of its scope) or touched by any Climate Advisor code. They add two new `climate.*`/`weather.*` entities and their own config entries — reversible by removing the integrations via Settings → Devices & Services and deleting the two folders. No interaction with any existing HA config, entity, or integration.
- **Resolution plan**: Not applicable — these are intentionally temporary dev/test tooling, not a production dependency. Remove both integrations (UI removal + folder deletion) once manual multi-zone testing is complete; no code change required in Climate Advisor itself.
- **Status**: Active — installed for ongoing multi-zone manual testing; safe to remove any time

### 3. AI Task: generate data and the limit lookup (Issue #1336)

- **Date**: 2026-11-20 (owner-approved)
- **What the occupant gets**: the AI Investigator runs on whatever AI provider they already set up in Home Assistant (OpenAI, Google, Anthropic, Ollama, OpenRouter, Nabu Casa Cloud, or a custom integration that offers AI Task). Climate Advisor keeps no key for it. When the AI settings are saved, and before each run, the occupant is warned if the provider's output limit or context window looks too small.
- **What**, and nothing more:
  - **One call:** `ai_task.generate_data`, through the Python helper `homeassistant.components.ai_task.async_generate_data(hass, task_name=..., entity_id=..., instructions=...)`, on the AI Task entity the owner picked in Climate Advisor's AI settings (`ai_task_entity`), or on HA's preferred AI Task entity (Settings > System > AI) when none is picked (`entity_id=None`). No `structure`, no attachments, no `llm_api`. Nothing else in `ai_task`: not `generate_image`, not any other service.
  - **One read-only lookup, for the limit check** (`ai_limits.py`): that entity's entity-registry entry (`config_entry_id`, `config_subentry_id`), then that subentry's options (`hass.config_entries.async_get_entry(id).subentries[sub_id].data`), plus HA's preferred AI Task entity (`gen_data_entity_id` in the `ai_task` preferences) to resolve an empty pick. The keys read are `max_tokens` (Anthropic, OpenAI, Google) and `num_ctx` (Ollama). They are internal to each integration, so a missing or renamed key means "can't check", never an error.
  - Nothing is written to the provider's integration, its config entry, its subentries or HA's AI preferences.
- **Why**: AI Task is Home Assistant's own pattern for "an integration needs an LLM, so let the user bring theirs". It replaces a Climate Advisor-owned key and per-provider code. The limit lookup is the only way to warn before a run: AI Task returns no token counts and no stop reason, and HA's Anthropic and OpenAI integrations raise an error at the output limit instead of returning a partial report.
- **Risk**: Low. One service call on an entity the owner chose, plus a read of settings the owner can already see in the provider's options. The settings keys are not a public HA API, so a provider change can only turn a warning into "can't check".
- **Resolution plan**: None needed while AI Task is HA's recommended path. Re-check the read keys at each HA minor release with the limit-reader unit tests.
- **Built-in Claude** (deprecated in the same issue) keeps calling the Anthropic cloud API directly with the owner's key until the release that removes it. The owner has not named that release yet. It stays listed in `claude.md` until then.
- **Status**: Active. Detail: [AI Integration Brief](ai-integration.md#provider-model-ai-task-first-issue-1336).

### 4. Worker Processes for Long Pure-Python Jobs (Issue #1722)

- **Date**: 2026-11-26 (owner-approved)
- **What the occupant gets**: Home Assistant no longer freezes for a minute or more while Climate Advisor learns. On a Raspberry Pi 4 the old in-process fit froze it for up to 90 s, which could reset a Zigbee stick and raise false "entity not responding" alerts.
- **What**, and nothing more:
  - Climate Advisor starts short-lived Python worker processes of the **same interpreter** (`sys.executable -I`, so neither `/config` nor `PYTHON*` variables are on the import path). No shell, no other program.
  - Only one worker at a time per job kind, under the locks that already serialise the work (the domain-wide v4 fit lock; the cycle lock).
  - A worker runs only a function named on the `process_offload.JOBS` allowlist, from a hass-free module of Climate Advisor's own package (`v4_shadow`, `night_replay`, `cycle_model`).
  - It talks only through its stdin and stdout pipes (JSON in, JSON out); its log lines come back on stderr and are relayed to Climate Advisor's logger.
  - It writes no files and makes no network or Home Assistant calls. It sees a stub of `homeassistant.util.dt` only (`now`, `utcnow`, `as_utc`).
  - It runs at nice +10 and is killed and reaped on timeout (`WORKER_TIMEOUT_*` in `const.py`), on unload and on HA stop.
  - If a process cannot be started (`OSError`), the same job runs in an executor thread and one WARNING is logged.
- **Why**: pure-Python CPU work in an executor thread holds the GIL and stalls the event loop, so "offloaded to the executor" is not enough for the v4 fit, the night replay and the cycle-model refit. A separate process has its own GIL. Measured on x86: loop I/O round-trips fell from about 64k/s to about 72/s during a threaded fit, and stayed at idle level with a worker.
- **Risk**: Low. The child runs only code Climate Advisor already ships, on data the coordinator passes it, and cannot reach Home Assistant. The parent applies every result. The risks are an extra interpreter's memory for a few seconds (a fit holds the data in two processes) and a worker that hangs: the timeout kills it, the old model or rates stay, and the next run retries. A platform that cannot start the process falls back to the old thread behaviour.
- **Resolution plan**: None needed while pure-Python fits stay this heavy. Review at each quarterly review: has a job become cheap enough to return to the executor, has HA gained a supported way to run CPU work off the loop, is any new job proposed (each one needs a name on the allowlist and a guard test; see [Process Offload Brief](process-offload-brief.md#adding-a-job)). Free-threaded Python would remove the cause; revisit when HA ships it.
- **Status**: Active. Detail: [Process Offload Brief](process-offload-brief.md).

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
