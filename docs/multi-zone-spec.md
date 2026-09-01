<!-- Nav: ← [docs/00-PROJECT-INSTRUCTIONS.md] | → [__init__.py#L411 | api.py#L72 | learning.py#L678 | state.py | chart_log.py | config_flow.py#L559 | automation.py#L9691] | ↔ [docs/02-ARCHITECTURE-REFERENCE.md] -->

# Multi-Zone Support — Territory Spec (Tier 3)

> **STATUS: Design proposal — not yet implemented.**

A "zone" is a second Climate Advisor config entry, not a new schema. The
automation engine and learning engine already construct one correct,
independent instance per config entry today — that part of multi-zone support
already works. What's missing is nine places where the code wrongly assumes
exactly one config entry will ever exist (persistence file collisions, REST
API "first entry" selection, destructive-service misdirection, panel/view
registration and its unload-time mirror, no zone-naming field). Fixing those
nine gaps is the entire scope of this document. A later feature letting zones
influence each other thermally is sketched and explicitly deferred — see
[Future: Zone Influence](#future-zone-influence-deferred-not-in-scope-for-implementation).

**This document does not authorize implementation.** See
[Prerequisites for Implementation](#prerequisites-for-implementation).

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What is a "zone" under this design? | A second Climate Advisor config entry, pointing at a second thermostat — not a new schema. | [Core Architecture](#core-architecture-a-zone-is-a-config-entry) |
| Why not a `zones: {}` dict inside one entry instead? | It still needs 6 of the 9 fixes below, relocated, plus it reinvents config-entry lifecycle management from scratch. | [Why config-entry-per-zone is still right](#why-config-entry-per-zone-is-still-right-despite-the-longer-gap-list) |
| What actually breaks with two config entries today? | Nine singleton assumptions across `__init__.py`, `api.py`, `state.py`, `chart_log.py`, `learning.py`, `config_flow.py`. | [The Nine Gaps](#the-nine-gaps) |
| What happens if a user adds a second zone before the dashboard is zone-aware? | Every existing dashboard/API caller keeps working via a deterministic fallback + WARNING + a native HA Repairs issue, instead of silently breaking. | [Transitional Safety Window](#transitional-safety-window) |
| Which gap is most severe? | Gap 5 — five HA services silently rebind to whichever entry set up last, including the destructive `reset_learning_data`. | [Gap 5](#gap-5--service-handler-misdirection-most-severe) |
| Is per-zone learning a separate feature to design? | No — it falls out for free once Gap 1 (entry-scoped `LearningEngine`) is fixed; `coordinator.py:464` already constructs one per entry. | [Resolved Questions](#resolved-questions) |
| What ships first? | The diagnostics hook and the test harness — both have zero dependencies and everything else benefits from having them in place first. | [Implementation Sequence](#implementation-sequence) |
| How does Gap 4's fix resolve "which zone" without inventing a second mechanism later? | A new `zone_registry.py` module (`get_coordinator`/`iter_coordinators`/`get_default_coordinator`) serves both the dashboard/API need now and the future cross-zone-read need. | [Gap 4](#gap-4--apipy-first-entry-selection-entire-rest-surface) |
| Is a later "zones affect each other" feature blocked by any in-scope fix? | No — `zone_registry.py`, Gap 7's `entry.title` requirement, and `storage_paths.py` all support it without modification. | [Future: Zone Influence](#future-zone-influence-deferred-not-in-scope-for-implementation) |
| How do I test this without real multi-zone hardware? | The current harness bypasses `async_setup_entry()` entirely, so Gaps 5/6/8/9's fixes have no automated regression test. A harness extension drives the real setup/unload path with two config entries. | [Testing Without Multi-Zone Hardware](#testing-without-multi-zone-hardware) |
| How do I get fast feedback from real multi-zone users? | A native HA `diagnostics.py` hook replaces the log-only `dump_diagnostics` service with a one-click downloadable bundle carrying multi-zone-specific fields, plus a symptom-to-gap triage checklist. | [Diagnostics and Field Feedback](#diagnostics-and-field-feedback) |
| What does each user-visible change actually look like? | Five mocked surfaces (naming field, entry list, Repairs card, diagnostics menu item, dashboard selector); mocking them surfaced two real refinements (conditional selector rendering, explicit Repairs card copy). | [UI Mocks](#ui-mocks) |
| What changes for a user, in plain terms? | A before/after table across eight areas, each tied to the design choice behind it. | [Outcomes: Before and After](#outcomes-before-and-after) |

## Scope

Which code section this spec covers.

- **Files:**
  - `custom_components/climate_advisor/__init__.py` — entry setup, service/view/panel registration
  - `custom_components/climate_advisor/api.py` — REST surface, coordinator resolution
  - `custom_components/climate_advisor/state.py` — `StatePersistence`
  - `custom_components/climate_advisor/chart_log.py` — `ChartStateLog`
  - `custom_components/climate_advisor/learning.py` — `LearningEngine`
  - `custom_components/climate_advisor/config_flow.py` — entry creation, zone naming
  - `custom_components/climate_advisor/automation.py` / `coordinator.py` — carried-over indoor-temp-read duplication (independent track, see [PR10](#implementation-sequence))
  - `custom_components/climate_advisor/zone_registry.py` (new) — Gap 4's fix; also the accessor surface a future Zone Influence feature would use
  - `custom_components/climate_advisor/storage_paths.py` (new) — Gaps 1-3's shared fix
  - `custom_components/climate_advisor/diagnostics.py` (new) — native HA diagnostics hook, see [Diagnostics and Field Feedback](#diagnostics-and-field-feedback)
  - `tools/sim_harness/ha_stubs.py`, `tools/sim_harness/fake_hass.py`, `tools/sim_harness/build_coordinator.py` (extended, harness-only — no production code) — see [Testing Without Multi-Zone Hardware](#testing-without-multi-zone-hardware)
- **Entry point:** `async_setup_entry()` in `__init__.py` — the per-entry construction path that already works correctly and that all nine gaps sit around.

What this spec does NOT cover: the frontend chart rendering internals
(`docs/02-ARCHITECTURE-REFERENCE.md` and `index.html`'s existing
`loadStatus()` pattern cover that; this spec only describes how it gets
parameterized by `entry_id` in PR9). Does not cover the thermal model itself
(`docs/thermal-model-v3-spec.md`) — per-zone learning reuses that model
unchanged, one instance per entry. Does not cover implementation of the
future Zone Influence feature itself — see
[Future: Zone Influence](#future-zone-influence-deferred-not-in-scope-for-implementation).

## Core Architecture: a zone is a config entry

Today, `async_setup_entry()` (`__init__.py`) already creates one
`ClimateAdvisorCoordinator` per config entry:

```python
# __init__.py:411
coordinator = ClimateAdvisorCoordinator(hass, dict(entry.data), entry_id=entry.entry_id)
```

Each coordinator already constructs its own `AutomationEngine` bound to that
entry's `climate_entity` (`coordinator.py:476-486`), with no shared/global mutable
state between instances. `LearningEngine` is likewise constructed per coordinator
(`coordinator.py:464`). Nothing in `manifest.json` or `config_flow.py`'s
`async_step_user` blocks a second config entry — no `single_config_entry` flag, no
`_async_abort_entries_match()` guard. A second Climate Advisor entry pointing at a
second thermostat is already mechanically possible today. **A zone's automation
and learning already work correctly per entry — this is standard Home Assistant
multi-device integration behavior, already present, not something to build.**

What is NOT yet safe is nine pieces of code that wrongly assume exactly one
config entry will ever exist. These are the actual scope of this feature — not a
new schema, but nine fixes.

### The Nine Gaps

Grouped by file/lifecycle phase (persistence, then API, then service
registration, then panel/view registration and its unload-time mirror, then
naming) — not by severity or build order. Severity is called out per gap;
[Gap 5](#gap-5--service-handler-misdirection-most-severe) is the single worst
finding despite sitting fifth in this grouping. Build order is in
[Implementation Sequence](#implementation-sequence).

#### Gap 1 — `LearningEngine` DB collision

`LearningEngine.__init__` (`learning.py:678`) takes only `storage_path`, and writes
to a fixed filename `LEARNING_DB_FILE = "climate_advisor_learning.json"`
(`const.py:278`) under `hass.config.config_dir`. Two entries collide — the second
entry's learning writes clobber the first's, or vice versa depending on save
timing.

#### Gap 2 — `StatePersistence` collision (same bug, second file)

`state.py`'s `StatePersistence` writes to a fixed filename `STATE_FILE =
"climate_advisor_state.json"` (`const.py:266`), with no entry-scoping. Same
collision shape as Gap 1.

#### Gap 3 — `ChartStateLog` collision (same bug, third file)

`chart_log.py`'s `ChartStateLog` writes to a fixed `_CHART_LOG_FILE`, with no
entry-scoping. Same collision shape as Gaps 1-2. Gaps 1-3 are grouped into a
single PR — see [Implementation Sequence](#implementation-sequence) — because they
are the same bug in three files, not three distinct designs.

**Fix design:**

- **New module: `custom_components/climate_advisor/storage_paths.py`** with one
  function:
  ```python
  def resolve_entry_scoped_path(config_dir: Path, base_filename: str, entry_id: str) -> Path:
      """Build an entry-scoped storage path, e.g. 'climate_advisor_learning.json' +
      entry_id -> 'climate_advisor_learning_<entry_id>.json'. Single source of truth
      for Gaps 1-3 so their filename-scoping scheme cannot drift across the three files
      the way the original bug already did."""
      stem, ext = base_filename.rsplit(".", 1)
      return config_dir / f"{stem}_{entry_id}.{ext}"
  ```
  Each of `LearningEngine.__init__` (`learning.py:678`), `StatePersistence.__init__`
  (`state.py:27-28`), `ChartStateLog.__init__` (`chart_log.py:44-45`) calls this
  once instead of hand-rolling its own path join. Verified safe against all
  three actual filenames (`STATE_FILE = "climate_advisor_state.json"`,
  `LEARNING_DB_FILE = "climate_advisor_learning.json"`, `_CHART_LOG_FILE =
  "climate_advisor_chart_log.json"` — `const.py:266,278`, `chart_log.py:24`) —
  each has exactly one `.`, so `rsplit(".", 1)` splits correctly.

  This is a **module, not a mixin** — same decision as
  [Shared-extraction module vs. mixin](#shared-extraction-module-vs-mixin-module-decided-not-an-open-question)
  below, applied to a second occurrence of the identical duplication shape.

#### Gap 4 — `api.py` first-entry selection, entire REST surface

`_get_coordinator(hass)` (`api.py:72-77`) returns `next(iter(entries.values()))` —
arbitrarily "the first" entry HA happens to hand back. This function is called
**21 times** across `api.py`, meaning the entire dashboard/REST API is blind to
any entry beyond the first. A user with two zones configured would see only one
zone's data no matter which zone they intended to view, with no error indicating
the second zone exists.

**Fix design:**

- **New module: `custom_components/climate_advisor/zone_registry.py`** — a small,
  stateless, dependency-light module (no HA subclassing), following the same
  precedent as `fan_status.py::resolve_untracked_fan_status()` (a plain function
  module already used in this codebase for a near-identical "3+ places need the
  same logic" problem, Issue #571/#510). Not placed in `api.py` (would force a
  future non-REST caller — the Zone Influence FSM sketched below — to import the
  REST layer) and not in `__init__.py` (setup/teardown orchestration only;
  nothing else in this codebase imports from `__init__.py`).
- Three functions:
  ```python
  def get_coordinator(hass, entry_id) -> ClimateAdvisorCoordinator | None:
      """Resolve one zone's coordinator by entry_id, or None if not found/unloaded."""
      return hass.data.get(DOMAIN, {}).get(entry_id)

  def iter_coordinators(hass) -> Iterable[ClimateAdvisorCoordinator]:
      """All currently-loaded zone coordinators."""
      return hass.data.get(DOMAIN, {}).values()

  def get_default_coordinator(hass) -> ClimateAdvisorCoordinator | None:
      """Single-zone convenience path. Returns the coordinator when exactly one zone
      is loaded; otherwise falls back to a deterministic first-entry selection with a
      WARNING log and a native HA Repairs issue (see repairs.py), rather than
      returning None outright — see Transitional Safety Window below."""
  ```
  `get_coordinator`/`iter_coordinators` serve Gap 4 (dashboard/API resolving the
  RIGHT entry) directly, and ALSO serve the future Zone Influence feature
  (enumerating siblings, resolving one by entry_id) — same underlying data
  (`hass.data[DOMAIN]`, confirmed `__init__.py:422`:
  `hass.data[DOMAIN][entry.entry_id] = coordinator`), one accessor surface
  designed for both consumers instead of two mechanisms built at different times.
- **How `entry_id` reaches the 21 `api.py` call sites**: a query parameter
  (`request.query.get("entry_id")`), not a URL path segment. `api.py:744`
  already uses a query parameter (`hours =
  float(request.query.get("hours", 12))`) — the only existing precedent in this
  file for a request-scoped parameter. `request.match_info` (the path-segment
  mechanism) has **zero** uses anywhere in `api.py` — a path-segment approach
  would require restructuring all 21 routes' URL patterns and `aiohttp`
  registration for the same outcome, a much larger diff that also isn't needed
  for Gap 4's actual bug (first-entry selection, not resource-identity
  modeling). Concretely: each handler adds `entry_id =
  request.query.get("entry_id")` at the top, passes it to
  `get_coordinator`/`get_default_coordinator`.

##### Transitional Safety Window

Nothing prevents a user from adding a second Climate Advisor entry before the
dashboard is zone-aware ([PR9](#implementation-sequence)) — a second entry is
already mechanically possible today via HA's native Add Integration flow, with
no dependency on any Climate Advisor PR shipping first. Once `api.py` is
entry-scoped ([PR7](#implementation-sequence)), any caller that doesn't send
an `entry_id` (every existing dashboard/API caller not yet updated for the new
query parameter) hits `get_default_coordinator()`'s multi-entry fallback.

**Design:** `get_default_coordinator()` does not return `None` when more than
one zone is loaded — it degrades to a deterministic first-entry selection (via
`hass.config_entries.async_entries(DOMAIN)`'s stable order, confirmed real HA
API, already used in this codebase at `repairs.py:38,77` — not
dict-iteration order, which is unstable across restarts), plus a WARNING log,
plus a native HA Repairs issue, so a log line alone isn't the only surface an
admin could miss.

**Mechanism:** reuse `homeassistant.helpers.issue_registry` via this
codebase's own `repairs.py` module, which already implements two Repairs
flows today (`WeatherEntityRepairFlow`, `ReloadNeededRepairFlow`), raised via
`ir.async_create_issue()` (confirmed real call sites: `__init__.py:386-395`
for `weather_entity_not_found`, `config_flow.py:643-651` for `reload_needed`)
and cleared via `ir.async_delete_issue()` (confirmed: `__init__.py:368,383,402`,
`repairs.py:44,80`). Both surface in HA's own **Settings → Repairs** list,
visible regardless of whether the CA dashboard panel is open.

**Boundary Rule basis:** issue-registry writes are scoped to the calling
integration's own `DOMAIN` — Climate Advisor can only create/delete issues
under its own domain, structurally incapable of touching anything outside its
scope — and this exact mechanism already ships in this codebase with prior
owner approval (the two existing issues above).

**Trigger and clear condition:** `len(hass.data[DOMAIN]) > 1`, evaluated at
two lifecycle points:

- **On raise**: at the end of `async_setup_entry()`, after
  `hass.data[DOMAIN][entry.entry_id] = coordinator` (`__init__.py:422`),
  recompute the zone count; if it's now `> 1`, call
  `ir.async_create_issue(hass, DOMAIN, "zone_resolution_ambiguous",
  is_fixable=False, is_persistent=True, severity=ir.IssueSeverity.WARNING,
  translation_key="zone_resolution_ambiguous")` — mirroring
  `weather_entity_not_found`'s shape, minus a guided fix flow (there's nothing
  for the user to configure).
- **On clear**: at the start of `async_unload_entry()`, after this entry is
  popped from `hass.data[DOMAIN]`, recompute the zone count; if it's now
  `<= 1`, call `ir.async_delete_issue(hass, DOMAIN, "zone_resolution_ambiguous")`.
  This is new code — `async_unload_entry()` (`__init__.py:545-562`) contains no
  `ir.*` call today.

**Scope of the signal:** `get_default_coordinator()`'s fallback is a
permanent, sanctioned feature for any caller that doesn't pass `entry_id` —
not solely a shim for the window before the dashboard ships. Once PR9 ships,
the dashboard stops hitting the ambiguous path (it sends `entry_id`), but a
direct API call, a user's own script, or a third-party tool integrating with
`api.py` without `entry_id` can still hit it, in any multi-zone install,
indefinitely. This issue is an ongoing informational signal tied to zone
count, clearing only when zone count drops back to one. (Gap 5's specific
danger — a destructive service call resolving ambiguously — is separately
closed by [PR4](#implementation-sequence)'s `vol.Schema` requirement that such
calls include an explicit zone identifier, per Error Conditions row 3; this
Repairs issue is about the read-path fallback, not that.)

**Translation strings:** add a `"zone_resolution_ambiguous"` entry to the
`"issues"` block in both `strings.json` and `translations/en.json`, matching
the shape of the existing `"weather_entity_not_found"`/`"reload_needed"`
entries.

**Card copy (added after mocking this surface — see [UI Mocks](#ui-mocks)):**
title `"Ambiguous zone selection"`, body `"Multiple Climate Advisor zones are
configured. Some requests that don't specify a zone may resolve to the wrong
one."` Kept short and occupant-facing — the technical trace (which entry_id
the fallback actually picked, and why) stays in the WARNING log line, not
duplicated onto the Repairs card. This mirrors the existing split between
`_LOGGER.warning()` detail and the shorter `weather_entity_not_found` card
text already in this codebase.

**Structural closure:** PR9 should ship in the same release batch as PR7
wherever practically possible, narrowing the window between merge and every
existing dashboard caller actually loading updated code. A `config_flow`
guard blocking second-entry creation until PR9 ships was considered and
rejected — it would add temporary gating machinery with no existing
precedent in this codebase, and contradicts Invariant 2 (no new gating
machinery for multi-zone).

#### Gap 5 — service-handler misdirection (most severe)

Five HA services — `respond_to_suggestion`, `force_reclassify`, `resend_briefing`,
`dump_diagnostics`, `reset_learning_data` (registrations at `__init__.py:451-456,
494-496, 509-514`) — are registered as closures capturing the `coordinator` local
variable bound at `__init__.py:411` for that specific `async_setup_entry` call.

`handle_reset_learning_data` (`__init__.py:503-507`) is a direct closure —
confirmed by reading its body:

```python
scope = call.data.get("scope", "all")
await hass.async_add_executor_job(coordinator.learning.reset, scope)
```

No `hass.data[DOMAIN][entry_id]` re-lookup, no entry/zone identifier in its schema
(`RESET_LEARNING_SCHEMA` only has `scope`). Because
`hass.services.async_register(DOMAIN, "reset_learning_data", ...)` is
domain-scoped, a second entry's setup **overwrites** the first entry's handler in
HA's service registry. All five services become permanently bound to whichever
entry's `async_setup_entry` ran most recently — including `reset_learning_data`, a
destructive action.

**Occupant-facing consequence:** calling `climate_advisor.reset_learning_data`
believing you're targeting the bedroom zone could silently wipe the living-room
zone's learned thermal model instead, with no error or warning. The user loses
weeks of accumulated thermal learning for the wrong room and has no indication
anything went wrong until that zone's automation starts behaving like a
fresh-install default again. This is not a visibility gap — it is silent
misdirection of a destructive action, and is the most severe item on this list.

#### Gap 6 — panel/view registration (needs empirical verification)

`__init__.py`'s setup order, confirmed by reading the function top-to-bottom:

1. coordinator creation (411)
2. `async_restore_state()` (414)
3. `coordinator.async_setup()` (417)
4. `async_config_entry_first_refresh()` (420 — coordinator/engine update loop now
   live and scheduled)
5. store in `hass.data` (422)
6. platform forward (425)
7. service registration (451-514)
8. REST view registration (517-518)
9. panel registration via `async_register_built_in_panel` (521-539 — fixed
   `frontend_url_path`, no reentrancy guard)

No guard exists anywhere before the view-registration loop or the panel
registration call. Home Assistant's frontend component is believed to raise
`ValueError` on a duplicate `frontend_url_path` unless called with `update=True`
(not passed here) — but this is not yet cited against HA's actual source or
confirmed by a live run, so treat it as a working hypothesis, not a fact.
**This specific runtime behavior needs empirical confirmation against a real/dev
HA instance, not just static reading** — because the
coordinator and `AutomationEngine` are already constructed and already running
their update loop (per the ordering above, steps 3-4 precede step 9) by the time
this potential crash would occur.

Two possible real-world outcomes, both must be planned for:

- **(a)** If the second entry's setup crashes cleanly with nothing left running,
  this is an ordinary blocking bug — fix by scoping `frontend_url_path`/view
  registration per entry.
- **(b)** If it crashes AFTER the coordinator/engine already started — which the
  confirmed ordering makes structurally possible — that is safety-critical: a
  second zone's automation would be live, running, and controlling real HVAC
  hardware with zero way for the user to see it, stop it, or reach it through the
  API/dashboard/services.

State explicitly: this must be checked before the fix is designed in detail, not
assumed either way. This is [PR3](#implementation-sequence).

#### Gap 7 — no zone-naming field exists

`config_flow.py:559` hardcodes `title="Climate Advisor"` unconditionally in
`async_create_entry` — confirmed no user-provided name field exists anywhere in
the flow. Two entries today are indistinguishable by title in HA's own
Settings → Devices & Services list, let alone in a future dashboard selector. This
blocks a dashboard zone-selector specifically (it needs a label to show), but does
**not** block any of the backend fixes in Gaps 1-6 above — those all key off
`entry.entry_id`, which already exists and is already unique, not off title.

**Zone naming is a prerequisite for TWO consumers, not one:** the planned
dashboard zone selector ([PR9](#implementation-sequence)), AND a future
zone-to-zone influence selector (a user needs to pick sibling zones by name,
e.g. "Bedroom" vs. "Living Room," not by raw `entry_id`) — see
[Future: Zone Influence](#future-zone-influence-deferred-not-in-scope-for-implementation).

**Hard requirement for PR8's implementation:** the name MUST be stored as
`entry.title`, readable via `hass.config_entries.async_get_entry(entry_id)` —
confirmed real, working precedent already in this codebase at
`coordinator.py:1495` (`entry = self.hass.config_entries.async_get_entry(self._entry_id)`)
— not as a bare cosmetic string with no accessor. If PR8 ships the name any
other way, the future zone-influence selector would need its own new read
path — genuine, avoidable rework. This is the one place in this document where
a specific implementation choice is mandated to prevent future rework, not left
as a style preference.

**Field default (added after mocking this surface — see [UI Mocks](#ui-mocks)):**
the name field must NOT default to `"Climate Advisor"` — pre-filling the exact
placeholder Gap 7 exists to get rid of would just let a user click through
and reproduce the bug this fix closes. Instead, suggest a default derived from
the selected `climate_entity`'s existing HA friendly name (already available
in the flow at the point the name field is shown, since `climate_entity` is
selected earlier in the same flow) — e.g. `climate.bedroom_thermostat`'s
friendly name "Bedroom Thermostat" suggests "Bedroom" — while leaving the
field editable so the user can confirm or override it. This is a suggestion
for a sensible default, not a validation rule; no uniqueness check against
sibling zone names is required (a duplicate name is a cosmetic annoyance, not
a functional bug — nothing in this document keys off `entry.title`
uniqueness).

#### Gap 8 — unguarded panel removal on unload

`async_unload_entry()` (`__init__.py:559`) calls `async_remove_panel(hass,
PANEL_FRONTEND_PATH)` unconditionally, with no guard checking whether other config
entries still exist. Contrast this directly with the very same function, six lines
earlier (`__init__.py:553`), which DOES guard the equivalent process-wide teardown:

```python
# __init__.py:550-554
if not hass.data[DOMAIN]:
    log_capture.uninstall(hass)

# __init__.py:557-559 — no equivalent guard
async_remove_panel(hass, PANEL_FRONTEND_PATH)
```

`log_capture.uninstall()` only runs once `hass.data[DOMAIN]` is empty, i.e. once the
last remaining entry is gone. `async_remove_panel()` has no such check — it fires on
every single unload, regardless of how many other entries remain.

**Occupant-facing consequence:** with two zones configured, deleting or reloading
EITHER zone silently removes the dashboard panel for BOTH zones. The surviving
zone's automation keeps running HVAC exactly as before, but the occupant loses all
visibility into it — no panel, no dashboard, no way to see status or issue a
service call — until that zone's own entry happens to reload and re-registers the
panel. This is the mirror image of Gap 6 (panel registration on setup) and belongs
in the same PR as that fix.

#### Gap 9 — services are never unregistered on unload

There is no `hass.services.async_remove` call anywhere in `async_unload_entry()`.
When zone B is deleted, the five domain-scoped services registered during zone B's
`async_setup_entry()` (Gap 5's closures) are never torn down — they simply continue
to exist in HA's service registry, still closed over zone B's now-shut-down
`coordinator`.

**Occupant-facing consequence:** if zone B was the entry whose setup happened to
register the currently-active service closures (Gap 5), deleting zone B and then
calling `climate_advisor.reset_learning_data` does not error and does not silently
redirect to the surviving zone — it silently acts on the deleted zone's now-defunct
`LearningEngine` instance, which is exactly the wrong outcome whether or not the
call appears to "succeed." This compounds Gap 5 (the live-misdirection bug) with a
teardown-time version of the same defect: destructive services have no reliable
binding to a zone that still exists, in either direction.

### Why config-entry-per-zone is still right despite the longer gap list

Considered alternative: reviving an earlier `zones: {zone_id: {...}}` dict-inside-
one-entry model, to check whether it would have fewer of these problems.

It would not, mostly. It still needs per-zone `LearningEngine`/`StatePersistence`/
`ChartStateLog` instances (Gaps 1-3, relocated inside one entry instead of spread
across entries). It still needs `api.py` to resolve "which zone" per request
(Gap 4, relocated). It still needs a zone-naming field (Gap 7, relocated). And it
still needs a way to route a service call to the intended zone (Gap 5's addressing
requirement — see below). It avoids **three** of the nine items outright — Gap 6
(panel registration on setup), Gap 8 (panel removal on unload), and Gap 9 (service
unregistration on unload) — because there would be only one `async_setup_entry`/
`async_unload_entry` call total, ever, so the collision/leak shape those three gaps
describe cannot occur.

Gap 5 deserves more precision than "same shape, different vector," since it is this
document's own most-severe finding. A single `async_setup_entry` call — which
the zones-dict model would have, since there would be only one config entry ever —
eliminates the **overwrite/misdirection** part of Gap 5: there is no second
`hass.services.async_register(DOMAIN, ...)` call to silently clobber the first, so
`reset_learning_data` can never rebind to "whichever zone set up last," because
nothing ever sets up a second time. What remains is only an **addressing**
requirement — which zone_id does this particular service call target — a real but
much smaller problem than silent misdirection of a destructive action. This is the
strongest point in favor of the zones-dict alternative, and it is a substantial one:
it removes the safety-critical half of the document's top finding essentially for
free, just by construction.

That is a real advantage, but it comes at the cost of reinventing config-entry
lifecycle management — add/remove/reconfigure a zone without HA's own native
add/remove/reload flow — from scratch. That is a bigger, more diffuse cost than the
three collisions it avoids, and it forfeits the free per-entry construction that
`coordinator.py:464` already gives every zone today. Config-entry-per-zone remains
the right model; it just means honestly treating "make `__init__.py`/`api.py`/the
persistence layer genuinely entry-scoped" as the real, larger body of work — not a
small patch bolted onto an already-correct design, and not a free win over the
alternative on Gap 5 either.

## Resolved Questions

Resolved via five-whys / reuse-of-existing-pattern reasoning.

### Per-zone learning: required from day one, not optional/speculative

Five-whys: a multi-zone user wants each room comfortable on its own terms → that
requires the automation engine's real decisions (bedtime setback, nat-vent
thresholds, comfort guards) to be based on THAT room's actual thermal behavior →
a single `k_passive` from one thermostat doesn't represent a different room's
insulation, sun exposure, or duct sizing → so "visibility only, learning later"
would mean either feeding a wrong zone's model into real control decisions, or
running that zone in a dumbed-down static mode and building the real learning path
later anyway — deferring doesn't reduce total work, it delays value while leaving
zones 2+ sharing zone 1's corrupted/blended model.

Once Gap 1 (entry-scoped `LearningEngine`) is fixed, every zone already gets
independent thermal-model fitting for free — that IS `coordinator.py:464`'s
existing per-entry construction, once its one bug is fixed. **There is no
additional "per-zone learning" feature to design; there is only the one existing
bug to fix, and fixing it is what delivers this.**

### Dashboard: a zone selector over the existing card layout, not a new comparison/aggregation card

The Status Card Ontology rule (project `CLAUDE.md`) requires the four existing
cards — Status, Next User Action, Next Automation, Automation Time — to never
answer each other's question. Bolting per-zone data onto those SAME cards for a
4-zone house would force each card to narrate multiple zones' state in one string
(e.g. Status saying "AC running in Living Room, grace period in Bedroom"
simultaneously) — the exact cross-card narration collision the ontology rule
exists to prevent, just at N-zone scale instead of the single card-vs-card scale
it was written for.

The correct read of "simplest and most effective for N zones" is that the
existing single-zone dashboard (4 cards + fan cards, `frontend/index.html`) is
already the right per-zone UI — it needs a zone selector (tabs, driven by each
entry's name once Gap 7 provides one) at the top of the panel, with the existing
`loadStatus()` and sibling functions (`index.html:884+`) parameterized by
`entry_id` and re-rendering the same card layout for whichever zone is selected.
This is N independent, selectable copies of the pattern that already works — not
a new aggregation layer.

**Conditional rendering (added after mocking this surface — see
[UI Mocks](#ui-mocks)):** the selector row must render only when
`zone_count > 1` (the same count already computed for the Transitional Safety
Window check, so no new counting logic — read it once, use it for both).
Nearly every existing install is single-zone; rendering an empty or
one-item tab row for all of them would be a visible, permanent UI change for
a feature that doesn't apply to them. With the guard, a single-zone dashboard
is pixel-identical to today's — the new chrome exists only for installs that
actually have something to select between.

<a id="redaction-resolved-by-existing-precedent-no-owner-decision-needed"></a>
### Redaction: resolved by existing precedent, no owner decision needed — but the precedent is field-type-specific, not blanket

The precedent has to be matched by field *type*, not by a single blanket claim,
because this codebase's existing `CONFIG_METADATA` loop (`api.py:535-561`,
`ClimateAdvisorConfigView.get()`) already treats strings and lists differently:

- **Plain-string fields** (e.g. `climate_entity`, a single entity_id, `category:
  "core"`, not flagged `sensitive` in `const.py`) pass through the loop unredacted.
  The only redaction check the loop applies is `if key == "notify_service" or
  meta.get("sensitive")` (`api.py:542`), which `climate_entity` doesn't match, so it
  falls through to `self.json(...)` as-is. This is the correct precedent for a
  zone's `climate_entity` and entry name: both are plain identifiers, same category
  as `notify_service` is NOT (an appliance/room identifier, not personal info), and
  both would be sent the same way `climate_entity` already is today.
- **List-typed fields** (e.g. `door_window_sensors`, an `EntitySelector(multiple=True)`
  list, `config_flow.py:414-419`/`928-936`) do **not** reach the frontend as raw
  entity_ids at all — confirmed `door_window_sensors` never appears anywhere in
  `api.py`. It only reaches the frontend through the same generic
  `CONFIG_METADATA` loop, which for any list-typed value does `if isinstance(value,
  list): value = f"{len(value)} configured"` (`api.py:548-549`) before the value is
  appended to the response. The entity_ids themselves are never transmitted — only
  a count is.

The actual transmission of `climate_entity` (and every other `CONFIG_METADATA`
field) happens in `ClimateAdvisorConfigView.get()`'s loop, `api.py:535-561`
(distinct from `api.py:100,115`'s `hass.states.get(...)` calls, which are a *use*
of the config value to look up live state, not the config-transmission path).

**Conclusion:** no redaction is needed for a zone's `climate_entity` or entry
name — they are plain strings, not `sensitive`-flagged, and would be sent
through the existing `CONFIG_METADATA` loop exactly as `climate_entity` already
is today. If this design ever introduces a zone field that is a LIST of entities
(e.g. per-zone door/window sensors), that field must NOT be assumed unredacted
by analogy to `climate_entity` — it follows the `door_window_sensors` precedent
instead (count-only, entity_ids never transmitted), which is a different
transport behavior for a different field type, not a redaction gap to design
around.

### Shared-extraction module vs. mixin: module, decided, not an open question

`ClimateAdvisorCoordinator` and `AutomationEngine` share no base class or
inheritance relationship elsewhere in the codebase — forcing a mixin onto them
would be an artificial relationship invented for this fix alone. The codebase
already has the right precedent for this exact problem shape:
`fan_status.py::resolve_untracked_fan_status()`, a stateless helper extracted
specifically to stop the same kind of cross-file duplication between
`_compute_fan_status()`/`_compute_whf_status()`/`_compute_hvac_fan_status()`.
Follow that pattern exactly for the indoor-temp-read fix (PR10, below).

## Future: Zone Influence (deferred, not in scope for implementation)

**Status:** Deferred. No implementation authorized by this section. Exists to
confirm the in-scope fixes above don't foreclose it.

**The feature:** letting a user configure which zones thermally influence each
other (e.g., "the living room's AC also cools the bedroom") and having
automation account for that.

**Config surface:** a multi-select field in the *influenced* zone's own options
flow, referencing sibling `entry_id`s (shown to the user by name, per Gap 7's
`entry.title` requirement above) — modeled structurally on the existing
`door_window_sensors` multi-entity-selector pattern (`config_flow.py:414-419`/
`928-936`, `EntitySelector(multiple=True)`), though the analogy is structural
only (siblings are zones, not entities; the selector would need to source its
options from `zone_registry.iter_coordinators()` filtered to exclude self, not
from an entity domain). Placed on the influenced zone (not the influencing
zone) because "what affects me" is the natural mental model for a user
configuring one room, and it keeps the read direction (self reads siblings)
consistent with the pull design below. A separate cross-zone-relationship
config entry or dedicated linking panel was considered and rejected — it would
duplicate config-entry lifecycle machinery this document already argued
against reinventing (see
[Why config-entry-per-zone is still right](#why-config-entry-per-zone-is-still-right-despite-the-longer-gap-list)).

**Data flow: pull, at the coordinator's existing update cadence, for decisions —
with a nuance for future thermal-parameter learning.** Five-whys, verified
against this codebase's own thermal model docs:

- Push (extending `lifecycle_dispatcher.py` — currently scoped one-per-
  `AutomationEngine` — to be domain-scoped) would need to additionally survive
  entries loading/unloading independently, i.e. new exposure to the exact same
  lifecycle-collision failure class as Gaps 6/8/9, which this whole document
  exists to close.
- Pull needs no registration/deregistration lifecycle at all — a coordinator
  that unloads simply stops appearing in `iter_coordinators()`'s next call,
  avoiding Gap 9's whole failure shape by construction.
- Checked specifically against the fastest cross-zone-relevant mechanism this
  codebase already models — whole-house-fan-driven air movement, tracked as
  `fan_only_decay`/`k_vent`, sampled every 2 minutes
  (`THERMAL_FAN_SAMPLE_INTERVAL_S = 120`, `const.py:1058-1059`) vs.
  `passive_decay`'s 5-minute sampling (`THERMAL_PASSIVE_SAMPLE_INTERVAL_S =
  300`) — confirmed the codebase's own comment marks this "faster signal." But
  `k_vent` is consumed only inside the ODE prediction curve
  (`_simulate_indoor_physics_v3`, `_build_predicted_indoor_future`), never by
  any faster-than-the-coordinator-cycle decision path — confirmed no
  real-time/immediate-setpoint code reads `k_vent`. So even the fastest
  cross-zone-relevant physical mechanism this codebase already tracks doesn't
  need decision-level latency faster than the existing ~30-min coordinator
  cycle.
- **Recommendation:** pull, at the existing coordinator cadence, for automation
  *decisions*. If a future cross-zone thermal parameter is ever learned (a
  `k_cross_zone` sibling to `k_vent`), that learning-side sampling should
  follow the `fan_only_decay` precedent (faster cadence when the source zone's
  fan is active) rather than the `passive_decay` cadence — but that's a
  modeling detail for whoever designs the feature in full, not a blocker here.

**Decision logic shape:** a new FSM, following this codebase's established
convention (frozen `*Inputs` dataclass, `*EventKind` enum, frozen `*Event`,
frozen `*Transition`, one pure `transition(current_state, event) -> Transition`
function, no hass/IO access inside it — matching `door_window_fsm.py` et al.).
The only structural difference from existing FSMs: `Inputs` would be built by a
wrapper method reading a SIBLING zone's `coordinator.data` snapshot via
`zone_registry.get_coordinator()`, instead of reading `self.<attribute>` — the
same shape as `DoorWindowFsmInputs.natural_vent_active`/`.whf_owns_hvac`
(`door_window_fsm.py:310-311`, already-existing "communicating automata"
reading another FSM's output), just resolved cross-instance instead of
intra-instance. The pure `transition()` function itself stays exactly as
hass-free/IO-free as every existing FSM.

**Verified: does anything in-scope need rework later?**

- `zone_registry.py` (Gap 4's fix, above): supports this without modification —
  `iter_coordinators`/`get_coordinator` are exactly the two primitives a
  cross-zone `Inputs`-builder needs.
- Gap 7's `entry.title` requirement (above): supports the "pick siblings by
  name" UI without modification, PROVIDED PR8 implements it as specified (an
  `entry.title`-based name, not a bare string).
- `storage_paths.py` (Gaps 1-3's fix, above): fully independent of
  zone-influence; no coupling either way.

**Genuinely open, not resolved here:** whether influence should be symmetric
(A↔B) or directional (A→B only) is a product decision the config-surface
sketch supports either way — deferred to whoever designs the feature in full.

## Implementation Sequence

Ordered by **build dependency** — each step lists only what it depends on
among the prior steps, so the sequence can be followed top to bottom without
rework. This is deliberately not severity order and not smallest-diff order:
Gap 5 (the single most severe finding) ships at step 4, not step 1, because
steps 1-2 have zero dependencies and give every later step something to be
debugged and tested with.

1. **PR1 — Diagnostics hook (`diagnostics.py`).** Zero dependencies. Ships
   first so every step below can be debugged with a real downloadable bundle
   from day one. See [Diagnostics and Field Feedback](#diagnostics-and-field-feedback).
2. **PR2 — Test harness: drive real two-entry setup/unload.** Zero
   production-code dependency. Required before PR4/PR5's fixes (both live
   inside `async_setup_entry()`/`async_unload_entry()`) can be
   regression-tested at all. See
   [Testing Without Multi-Zone Hardware](#testing-without-multi-zone-hardware).
   Its build starts concurrently with PR3, not after PR3 finishes — the
   harness is written to model both of PR3's possible outcomes as parallel
   test variants, so PR5's design work isn't blocked on PR3 completing first.
3. **PR3 — Empirical spike, no shipped code.** Stand up two config entries
   against a real/dev HA instance. Resolve Gap 6: does `async_setup_entry`
   raise on the second entry (duplicate `frontend_url_path`)? If yes, is the
   first coordinator's update loop still running afterward (check
   `hass.data[DOMAIN]` and logs)? This gates PR5's exact design. Also the
   natural point to resolve the OPEN QUESTION from
   [Testing Without Multi-Zone Hardware](#testing-without-multi-zone-hardware) —
   whether `hass.services` exposes closure/coordinator identity
   introspectably enough for `service_registry_binding`/
   `active_service_bindings` — since it's the same "confirm against a real
   instance, not static reading" category of unknown.
4. **PR4 — Service-handler scoping and unregistration (Gaps 5 and 9).**
   Safety-critical; no dependency on PR3's result. Make service registration
   per-entry-safe (entry_id-suffixed service names, or a required
   zone-identifying parameter routed through a single domain-wide
   registration guarded against double-registration), and add the missing
   `hass.services.async_remove` calls to `async_unload_entry()` so a deleted
   zone's service closures cannot linger and be silently called against a
   defunct coordinator. Ships as soon as PR2's harness can validate it.
5. **PR5 — Panel/view registration scoping on setup and unload (Gaps 6 and
   8).** Design depends on PR3's empirical result: if PR3 confirms outcome
   (a) (clean crash, nothing left running), the fix is a straightforward
   per-entry-scoped `frontend_url_path`/view registration; if PR3 finds
   outcome (b) instead (crash after the coordinator/engine already started),
   the fix additionally requires reordering `__init__.py` so
   panel/service/view registration happens BEFORE
   `coordinator.async_setup()`/first refresh, so a registration failure
   aborts before any control loop begins running. Either way,
   `async_unload_entry()` also needs the Gap 8 guard —
   `async_remove_panel()` must not fire unless `hass.data[DOMAIN]` is empty,
   mirroring the existing `log_capture.uninstall()` guard six lines above it.
6. **PR6 — Entry-scoped persistence (Gaps 1-3).** No dependency on PR1-PR5.
   `LearningEngine`, `StatePersistence`, `ChartStateLog` all take
   `entry.entry_id` into their filename via `storage_paths.py`, each with a
   one-time migration mapping existing single-entry data to that entry's new
   scoped filename.
7. **PR7 — `api.py` entry-scoping + zone registry + Transitional Safety
   Window (Gap 4).** Needs PR6's entry-scoped backing stores to select
   between. Ships the new `zone_registry.py` module
   (`get_coordinator`/`iter_coordinators`/`get_default_coordinator`) and
   replaces `_get_coordinator()`'s first-entry selection with entry_id-aware
   resolution (a query parameter, per `api.py:744`'s precedent) across all 21
   call sites. Also ships the
   [Transitional Safety Window](#transitional-safety-window) fix.
8. **PR8 — Config-flow zone naming (Gap 7).** No hard dependency, but has no
   consumer until PR9. Add a name field stored as `entry.title` (per the hard
   requirement above — not a placeholder string with no accessor), so PR9
   and the future zone-influence selector both have something real to select
   on.
9. **PR9 — Dashboard zone selector.** Depends on PR4-PR8 all being zone-safe
   and named — the first PR that assumes the backend is actually zone-safe.
   Ship in the same release batch as PR7 wherever practically possible, to
   close the Transitional Safety Window quickly. Renders only when
   `zone_count > 1` — see the conditional-rendering note under
   [Resolved Questions](#dashboard-a-zone-selector-over-the-existing-card-layout-not-a-new-comparisonaggregation-card).
10. **PR10 (independent track, no dependency on PR1-PR9) — the
    automation.py/coordinator.py shared indoor-temp-read fix.** Independently
    valuable, no dependency on the zone work — fixes a live single-zone bug
    today. Can ship whenever convenient, before or after the rest of this
    list. See [Carried-Over Citations](#carried-over-citations).

## Testing Without Multi-Zone Hardware

The project owner has no second physical HVAC zone, so nothing above can be
personally validated end-to-end without either buying hardware or a way to test
it headlessly.

### Why this is a real gap

The existing simulation harness (`tools/sim_harness/build_coordinator.py:181`,
`build_headless_coordinator()`) constructs `ClimateAdvisorCoordinator(fake_hass,
merged_config)` **directly** — it does not go through `async_setup_entry()` at
all. Confirmed: no `entry_id=` kwarg, no `hass.data` write, no service
registration, no platform forward (the comment at `build_coordinator.py:177-180`
confirms `__init__` never touches `async_track_*`/`hass.bus`). Consequence:
**Gaps 5, 6, 8, and 9 live entirely inside `async_setup_entry()`/
`async_unload_entry()` (`__init__.py`) — code this harness never executes.**
None of those four gaps' fixes have any automated regression test today, or
would after PR4/PR5 ship, without harness changes.

### Harness extension needed

Two additions to `tools/sim_harness/`, no production code touched:

1. **`ha_stubs.py`: add a config-entry stub.** Today only `ConfigFlow`/
   `OptionsFlow` are realified (confirmed: lines 179-229 define
   `_MockConfigFlow`/`_MockOptionsFlow`; wired at lines 317-319). A whole-file
   grep for "ConfigEntry" turns up exactly one hit — a docstring reference to
   `ConfigEntryNotReady` — no config-entry-shaped stub exists. Add a minimal
   stub exposing `.entry_id`, `.data`, `.title`, `.options={}` — enough for
   `async_setup_entry`/`async_unload_entry` to run against, not HA's full
   `ConfigEntry` state machine.
2. **`FakeHass` needs `.data` and `.config_entries`.** Confirmed: zero matches
   for either attribute in `fake_hass.py` today (one incidental unrelated hit,
   `event.data.get(...)`). `.data` must be a real dict so
   `hass.data.setdefault(DOMAIN, {})` (`__init__.py:354`) and
   `hass.data[DOMAIN][entry.entry_id] = coordinator` (`__init__.py:422`) work
   unmodified. `.config_entries` needs `async_forward_entry_setups` (no-op),
   `async_unload_platforms` (no-op, returns `True`), and `async_entries(DOMAIN)`
   returning stub entries in stable order — the exact accessor the Transitional
   Safety Window fallback depends on in production (`repairs.py:38,77`,
   confirmed: both lines read `self.hass.config_entries.async_entries(DOMAIN)`
   verbatim), so the fake must model this faithfully, not stub it away.
3. **`build_coordinator.py`: add `build_headless_multi_zone(zone_count=2, ...)`.**
   Calls the REAL `async_setup_entry(fake_hass, entry)` per zone against one
   shared `fake_hass`, unlike `build_headless_coordinator()`'s direct-
   construction shortcut. Returns all coordinators plus the shared `fake_hass`,
   so a test can assert against `fake_hass.data[DOMAIN]` directly — the same
   shape `tests/test_api.py:43-60` already hand-builds via `MagicMock()`
   (`hass.data = {DOMAIN: {"entry_1": coord}}`), now produced by the real setup
   path instead of a hand-built stand-in. `build_headless_coordinator()` itself
   is untouched — single-zone tests keep the fast path; multi-zone tests opt
   into this slower, more faithful one only when needed.

### Golden scenario schema extension

Additive, not a breaking migration. A new optional top-level `"zones": [...]`
array — each element carries its own `climate_entity`, `config`, and `events`,
matching one real config entry. Existing scenarios (no `"zones"` key) are
unaffected. Three new assertion types:

- **`cross_zone_isolation`** — e.g. `{"type": "cross_zone_isolation",
  "action_zone": "zone_a", "service": "reset_learning_data",
  "unaffected_zone": "zone_b", "unaffected_field":
  "learning.thermal_model.k_passive"}` — call a service scoped to zone A,
  assert zone B's state for the named field is unchanged. This is the literal
  Gap 5/9 bug class.
- **`service_registry_binding`** — e.g. `{"type": "service_registry_binding",
  "service": "reset_learning_data", "expected_target_entry_id": "zone_a"}` —
  after both zones' setup, assert the currently-registered service closure is
  actually bound to the entry the test expects, not just that some call
  succeeded.
- **`teardown_cleanup`** — e.g. `{"type": "teardown_cleanup", "unload_entry":
  "zone_b", "expect_services_present": true, "expect_panel_present": true}` —
  unload one zone, assert the surviving zone's services (Gap 9) and panel
  (Gap 8) are still present, not silently removed.

**OPEN QUESTION:** whether `hass.services` exposes closure/coordinator identity
introspectably enough at runtime to actually implement `service_registry_binding`
and the `active_service_bindings` diagnostics field (see
[Diagnostics and Field Feedback](#diagnostics-and-field-feedback) below) without
relying on undocumented HA internals. Requires PR3-adjacent empirical
verification; not resolved here.

### Pre-condition 2

The harness must gain two-entry setup/teardown capability
([PR2](#implementation-sequence), above) before PR4's fix can be
regression-tested — this is new harness capability, not a scenario audit
(confirming "0 of 0 scenarios assume single-entry shape" would be vacuously
true today precisely because the harness cannot construct a second entry at
all).

### Relationship to PR3's manual spike

PR3 stays a manual empirical spike against a real HA instance — a headless
`FakeHass` cannot exercise real HA's `async_register_built_in_panel`/frontend-
component internals, so it cannot answer PR3's specific question (does a
duplicate `frontend_url_path` registration raise, and if so, before or after
the coordinator's update loop is live). But don't run this fully serially:
**write PR2's harness to model BOTH of PR3's possible outcomes as parallel
test variants** (one assuming outcome (a) — clean crash, nothing left running;
one assuming outcome (b) — crash after the coordinator/engine already started)
rather than waiting for PR3's result to determine a single harness design. This
lets PR5's fix be drafted and reviewed against both possible shapes
concurrently with PR3 running, shortening the critical path.

## Diagnostics and Field Feedback

### The gap in the existing mechanism

`dump_diagnostics` (`__init__.py:474-488`, confirmed exact fields: `version`,
`timestamp`, `debug_state` via `coordinator.get_debug_state()`, chart-data
point-count summary, `learning_summary` via `get_compliance_summary()`,
`config` excluding `notify_service`, `briefing_state`) does exactly one thing
with this payload: `_LOGGER.info(...)`. No file write, no HA notification, no
download — a user has to call the service via Developer Tools, then manually
dig the JSON blob out of HA's log viewer and copy/paste it. Not a realistic
"attach this to a bug report" flow. Confirmed via full-repo grep: no
`diagnostics.py` file and no `async_get_config_entry_diagnostics` hook exist
anywhere in this integration today — HA's standard, native "Download
Diagnostics" button (Settings → Devices & Services → entry → Download
Diagnostics) is unavailable.

### Fix: implement HA's native diagnostics hook

New file `custom_components/climate_advisor/diagnostics.py`, `async def
async_get_config_entry_diagnostics(hass, entry) -> dict`. **No frontend code
is written for this** (confirmed after mocking this surface — see
[UI Mocks](#ui-mocks)): implementing the hook is sufficient for HA to show its
own standard "Download Diagnostics" item in the entry's existing kebab menu
(Settings → Devices & Services → entry → ⋮) — the button itself is native HA
chrome, not something this integration builds or maintains. Payload, tailored
specifically to make a multi-zone bug report immediately diagnosable rather
than requiring back-and-forth:

- `version` — reused from `dump_diagnostics`
- `zone_count`: `len(hass.data[DOMAIN])` — immediately tells a triager whether
  this is a single- or multi-zone report
- `this_entry_id`, `entry_title` — flags whether `entry.title` is still the
  Gap 7 placeholder (`"Climate Advisor"`, hardcoded unconditionally at
  `config_flow.py:559`) or a real user-set name; a placeholder title in a
  report is itself signal that this install predates PR8's fix
- `entry_setup_order` — this entry's position in
  `hass.config_entries.async_entries(DOMAIN)`'s stable order, the same
  accessor the Transitional Safety Window fallback uses in production —
  directly diagnostic for Gap 5/9-class reports
- `active_service_bindings` — which entry_id each of the five domain-scoped
  services is currently bound to, if introspectable (flagged above as needing
  empirical confirmation, see the OPEN QUESTION in
  [Testing Without Multi-Zone Hardware](#testing-without-multi-zone-hardware))
  — the single most direct diagnostic for "I called reset_learning_data on
  zone B and zone A's data changed"
- The existing `dump_diagnostics` fields (`debug_state`, chart-data counts,
  `learning_summary`, `config` minus `notify_service`, `briefing_state`)

**Redaction:** reuse the existing `CONFIG_METADATA`/`api.py:542` convention
(`if key == "notify_service" or meta.get("sensitive")`) already established
and verified above — none of the new fields above are sensitive-flagged or
personal. Wrap the final payload in HA's own `async_redact_data(data,
TO_REDACT)` helper as defense-in-depth, listing `notify_service` and any
`sensitive`-flagged keys — consistent with existing practice, not a second
redaction mechanism.

**`dump_diagnostics` service: keep it, redirect it, don't deprecate it.**
Change `handle_dump_diagnostics` to build its payload via the same helper
function `diagnostics.py` uses (single source of truth for the payload
shape), and keep logging it too (current behavior, for continuity — some
users may have automations that already call this service).

### HA Boundary Rule check

`diagnostics.py` lives inside `custom_components/climate_advisor/`, reads only
this integration's own coordinator/config state, and HA's native diagnostics
hook (`async_get_config_entry_diagnostics`, per Home Assistant's own developer
docs — [Implements diagnostics](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics/))
is HA-initiated: the user clicks Download Diagnostics, HA generates and
downloads the file client-side — nothing is transmitted anywhere
automatically. `docs/HA-BOUNDARY-EXCEPTIONS.md` (one active exception, the
learning-DB file) needs no new entry for this.

### Fast triage: symptom-to-gap mapping

A GitHub issue template (design sketch only — not written here) should ask, as
checkboxes, for symptoms that map directly to a specific gap, so a report is
triageable on sight:

- "Learning data / chart history looks wrong or reset unexpectedly after
  adding a second zone" → Gaps 1-3
- "A service call (especially reset_learning_data) affected the wrong zone, or
  affected both zones" → Gap 5 — flagged for immediate triage priority, since
  it's this document's most severe finding
- "Dashboard/API only ever shows one zone's data, regardless of which you
  expect" → Gap 4
- "Second zone's setup failed, or the dashboard panel disappeared after
  adding/removing a zone" → Gaps 6/8
- "A service still seems to work after deleting a zone, acting on stale data"
  → Gap 9
- "Both zones show the same name, or an unhelpful default name" → Gap 7
- A required field: attach the new Download Diagnostics output for the
  affected zone — this alone answers most of the above without back-and-forth.

### Release labeling, no new channel

Consistent with this project's existing flat release process (every PR goes
through the same version-bump/fix_history/CHANGELOG/PR/merge flow — no beta or
hotfix lane exists, confirmed by reading the Release Process section of
CLAUDE.md), do not invent a new channel. The release that ships PR9 (dashboard
zone selector — the user-visible "multi-zone is here" milestone) gets an
explicit CHANGELOG/GitHub Release callout (e.g. "Multi-Zone Support
(Experimental)") using the existing `fix_history.jsonl --user-summary`/
CHANGELOG mechanism, encouraging early adopters to watch subsequent patches
and use the symptom checklist above when filing issues. A labeling convention,
not new infrastructure.

## UI Mocks

Five surfaces actually change what a user sees. Mocked below in text form for
this doc; a rendered version lives in the published HTML review artifact.
Building these surfaced five refinements, folded into the relevant sections
above and cross-referenced from each mock below — this section doesn't
introduce new decisions, it's the trace of where each one came from.

### Mock 1 — Add Integration flow: zone naming (Gap 7 / PR8)

```
┌─ Add Integration: Climate Advisor ─────────────────────┐
│ Thermostat:  [ climate.bedroom_thermostat          ▾ ] │
│                                                         │
│  BEFORE (today)                                        │
│    — no name field —                                   │
│    entry is silently titled "Climate Advisor"           │
│                                        [ Submit ]       │
│                                                         │
│  AFTER (PR8)                                            │
│    Zone name                                            │
│    [ Bedroom                                        ]   │
│    suggested from climate.bedroom_thermostat's          │
│    friendly name — editable, not required to be unique  │
│                                        [ Submit ]       │
└──────────────────────────────────────────────────────────┘
```

Finding from this mock: a name field alone isn't enough — its *default value*
matters. See the "Field default" note under
[Gap 7](#gap-7--no-zone-naming-field-exists).

### Mock 2 — Settings → Devices & Services entry list (Gap 7, no new code)

```
BEFORE                              AFTER
┌───────────────────────┐           ┌───────────────────────┐
│ Climate Advisor        │           │ Bedroom                │
│ 1 device                │           │ 1 device                │
└───────────────────────┘           └───────────────────────┘
┌───────────────────────┐           ┌───────────────────────┐
│ Climate Advisor        │           │ Living Room             │
│ 1 device                │           │ 1 device                │
└───────────────────────┘           └───────────────────────┘
   indistinguishable                    distinct, by entry.title
```

Finding from this mock: this list is entirely native HA chrome, driven off
`entry.title` — there is nothing to build here beyond Gap 7's fix itself.
Worth stating explicitly so this doesn't get mistaken for a page this
integration needs its own code for.

### Mock 3 — Settings → Repairs (Transitional Safety Window / PR7)

```
BEFORE:  (nothing appears — a caller with no entry_id silently gets an
          arbitrary zone, with no visible signal anywhere)

AFTER:
┌─ Settings → Repairs ─────────────────────────────────────┐
│ ⚠ WARNING                                                │
│ Ambiguous zone selection                                 │
│ Multiple Climate Advisor zones are configured. Some       │
│ requests that don't specify a zone may resolve to the     │
│ wrong one.                                                │
│                                          [ Learn more ]   │
└────────────────────────────────────────────────────────────┘
```

Finding from this mock: the card needed its own short, occupant-facing copy —
distinct from the WARNING log line's technical detail. See "Card copy" under
[Transitional Safety Window](#transitional-safety-window).

### Mock 4 — Entry menu: Download Diagnostics (PR1, no new frontend code)

```
Settings → Devices & Services → Bedroom

BEFORE                      AFTER
⋮ Reload                    ⋮ Reload
  Disable                     Disable
  Delete                      Download diagnostics   ← native HA menu item
                               Delete

Downloaded file (excerpt):
{
  "zone_count": 2,
  "this_entry_id": "01J...bedroom",
  "entry_title": "Bedroom",
  "entry_setup_order": 1,
  "active_service_bindings": { "reset_learning_data": "01J...bedroom", ... },
  ...
}
```

Finding from this mock: confirmed there is no frontend surface to design —
implementing the hook is the entire deliverable. See the note in
[Diagnostics and Field Feedback](#fix-implement-has-native-diagnostics-hook).

### Mock 5 — Dashboard: zone selector (PR9)

```
BEFORE / single-zone install (unchanged, before or after PR9 ships):
┌─────────────────────────────┐
│ Climate Advisor              │
│  (no zone-selector row)      │
│ Status ...                   │
│ Next User Action ...         │
│ Next Automation ...          │
│ Automation Time ...          │
└─────────────────────────────┘

AFTER / two-or-more-zone install only:
┌─────────────────────────────────────┐
│ Climate Advisor                      │
│  [ Bedroom ] [ Living Room ]         │  ← new, zone_count > 1 only
│ Status ...            (scoped to     │
│ Next User Action ...   selected tab) │
│ Next Automation ...                  │
│ Automation Time ...                  │
└─────────────────────────────────────┘
```

Finding from this mock: mocking a single-zone install next to a multi-zone
one made it obvious the selector row must not appear at all below
`zone_count > 1` — otherwise every existing single-zone dashboard gets a
permanent, pointless piece of new chrome. See "Conditional rendering" under
[Resolved Questions](#dashboard-a-zone-selector-over-the-existing-card-layout-not-a-new-comparisonaggregation-card).

### Review: accuracy, completeness, simplicity, DRY

- **Accuracy** — each mock's "after" state was checked against the specific
  fix design it illustrates (Gap 7's `entry.title` mechanism, the Repairs
  `ir.async_create_issue()` shape, the diagnostics hook's native menu
  placement, PR9's card-reuse design) rather than drawn freestyle; none
  invents a UI element not already specified elsewhere in this document.
- **Completeness** — five user-visible surfaces exist across the ten PRs
  (PR1, PR7, PR8, PR9, and Gap 7's native list); all five are mocked. PR2-PR6
  and PR10 have no user-visible surface (persistence, service registration,
  panel-registration internals, the indoor-temp fix) — nothing to mock there.
- **Simplicity** — two of five mocks (2 and 4) resolved to "no new UI to
  build," which is itself a simplicity finding worth keeping visible rather
  than quietly dropping those rows.
- **DRY** — the two real design refinements that came out of this pass
  (conditional zone-selector rendering, sharing one `zone_count` computation
  between the Repairs check and the dashboard guard) both reduce code rather
  than add it — no new UI pattern was introduced anywhere; every "after" mock
  reuses an existing HA-native surface (kebab menu, Repairs list, entry list,
  config-flow field, existing dashboard cards) rather than inventing a new one.

## Outcomes: Before and After

| Area | Before (today) | After (this proposal ships) | Design choice made |
|---|---|---|---|
| Adding a second zone | Visible only in HA's own entry list, indistinguishable name; dashboard/API silently shows only one zone's data with no indication a second exists | Named zone, own dashboard tab, own API responses, independently managed | Reuse HA's native multi-config-entry mechanism — no bespoke `zones` schema (see [Core Architecture](#core-architecture-a-zone-is-a-config-entry)) |
| Zone identity | Every zone titled "Climate Advisor" | User-chosen name (e.g. "Bedroom"), suggested from the thermostat's own friendly name | Store as `entry.title`, not a placeholder string, so later features (dashboard, zone-influence) read it for free ([Gap 7](#gap-7--no-zone-naming-field-exists)) |
| `reset_learning_data` and other destructive commands | Could silently rebind to whichever zone set up last; wrong zone's data wiped with no error | Always requires and honors an explicit zone identifier; rejected at the schema level if omitted | `vol.Schema` requirement, not a UI change — enforced before the handler runs ([Gap 5](#gap-5--service-handler-misdirection-most-severe), Invariant 3) |
| Second zone added before the dashboard update ships | Nothing — every existing dashboard/API caller silently gets an arbitrary zone | Settings → Repairs shows an explicit, short warning until zone count drops back to one | Reuse the native Repairs system already shipping two other issues in this codebase, instead of a notification call or a dashboard-only field ([Transitional Safety Window](#transitional-safety-window)) |
| Deleting a zone | Could remove the dashboard panel for *both* zones and leave stale commands still callable | Only removes what belonged to the deleted zone; the surviving zone's panel and commands are untouched | Mirror the existing single-file teardown guard already used for `log_capture.uninstall()`, six lines above the unguarded call ([Gap 8](#gap-8--unguarded-panel-removal-on-unload), [Gap 9](#gap-9--services-are-never-unregistered-on-unload)) |
| Filing a bug report | `dump_diagnostics` writes to HA's internal log only — has to be manually dug out and pasted | One-click "Download Diagnostics" produces an attachable file with multi-zone-specific fields (zone count, setup order, service bindings) | Implement HA's standard diagnostics hook — no custom export mechanism, no new frontend code ([Diagnostics and Field Feedback](#diagnostics-and-field-feedback)) |
| Dashboard for today's single-zone installs | One panel, no selector | **Unchanged** — pixel-identical to today | Selector renders only when `zone_count > 1`, found while mocking [Mock 5](#mock-5--dashboard-zone-selector-pr9) |
| Per-zone thermal learning | Shared/overwriting learning database across zones | Fully independent thermal model per zone | Fix the one storage-path bug ([Gaps 1-3](#gap-1--learningengine-db-collision)) rather than building a new "per-zone learning" feature — it falls out for free |

## Pre-conditions

What must be true before this work begins:

1. PR3's empirical spike has run against a real/dev HA instance and Gap 6's crash
   behavior (outcome (a) or (b)) is confirmed, not assumed.
2. The harness must gain two-entry setup/teardown capability
   ([PR2](#implementation-sequence), see
   [Testing Without Multi-Zone Hardware](#testing-without-multi-zone-hardware))
   before PR4's fix can be regression-tested — this is new harness capability,
   not a scenario audit.
3. A backup/rollback plan exists for the PR6 migration step (renaming existing
   `climate_advisor_learning.json`/`climate_advisor_state.json`/chart-log files to
   an entry-scoped name) so an interrupted migration cannot leave a user with
   neither the old nor the new file readable.

## Post-conditions

What is guaranteed to be true after PR1-PR10 all ship:

1. Two (or more) Climate Advisor config entries can be added via HA's native
   "Add Integration" flow, each independently named, each with its own
   thermostat, sensors, learning DB, state file, and chart log.
2. Every HA service (`reset_learning_data` included) unambiguously targets the
   zone the user intended, with no cross-zone bleed from registration-order
   collisions.
3. `api.py`'s REST surface and the dashboard's zone selector can address any
   configured zone, not just "whichever entry HA iterates first."
4. Removing a zone (via HA's native "Delete" on that config entry) does not
   corrupt or delete another zone's persisted data.
5. A user can produce a downloadable, per-zone diagnostics bundle (via HA's
   native Download Diagnostics button) without needing to call a service and
   dig the result out of the log — see
   [Diagnostics and Field Feedback](#diagnostics-and-field-feedback).

## Invariants

Properties that hold throughout, not just before/after:

1. The automation engine and learning engine remain one instance per config
   entry, constructed exactly as they are today (`coordinator.py:464`,
   `coordinator.py:476-486`) — no shared mutable state is introduced between
   zones as part of this work.
2. No new `zones` dict, config key, or schema is introduced anywhere in
   `const.py`/`config_flow.py`. Multi-zone support is expressed entirely through
   HA's existing multi-config-entry mechanism.
3. A destructive service call (`reset_learning_data`) always requires and honors
   an explicit zone/entry identifier once PR4 ships — it can never again silently
   resolve to "whichever entry set up last."

## Error Conditions

| Failure | Handling | Caller receives |
|---|---|---|
| Second entry's `async_register_built_in_panel` collides with the first (Gap 6, outcome pending PR3) | **If PR3 confirms outcome (a)** (clean crash, nothing left running): per-entry-scoped `frontend_url_path`/view registration (PR5). **If PR3 confirms outcome (b)** (crash after coordinator/engine already started): the same scoping fix, plus reordering `__init__.py` so panel/service/view registration happens before `coordinator.async_setup()`/first refresh, so a registration failure aborts before any control loop begins (PR5) | Entry setup fails cleanly with a clear HA-visible error, no partially-running automation |
| Two entries both attempt to write persisted state on startup before PR6 migration completes | Migration must be idempotent and atomic (existing `os.replace` pattern in `state.py`/`chart_log.py` reused, not reinvented) | Neither file is left in a half-written state |
| A service call omits the (post-PR4) required zone/entry identifier | `vol.Schema` validation rejects the call before the handler runs (per Security Requirements — CLAUDE.md) | `vol.Invalid` error, no action taken |
| `api.py` receives an explicit `entry_id` for a zone that has since been removed | Explicit "zone not found" response, not a silent fallback to another zone (distinct from the no-`entry_id` case, which uses `get_default_coordinator`'s deterministic fallback per the Transitional Safety Window fix, not a 404) | 404-shaped API error, not stale/wrong-zone data |

## User Scenarios

### Occupant-facing

You add a second Climate Advisor entry the same way you added the first: **Settings
→ Devices & Services → Add Integration → Climate Advisor**, pointing it at the
bedroom's thermostat instead of the living room's. Climate Advisor manages it
exactly like your first zone — the same automated setpoints, the same nat-vent and
comfort-guard logic, and its own independent thermal learning that improves over
time based on the bedroom's actual behavior, not the living room's. Your dashboard
gains a tab (or selector) to switch between zones; each tab shows the same Status /
Next User Action / Next Automation / Automation Time cards you already know, scoped
to whichever zone is selected. Calling a service like "reset learning data" from
the bedroom zone's card only ever touches the bedroom's learned model.

### Developer-facing

The [Implementation Sequence](#implementation-sequence) above is the whole of
the work. What does **not** change: the `AutomationEngine`/`LearningEngine`/
coordinator classes themselves, the thermal model, the classifier, the
briefing generator, and the HA config-entry lifecycle (add/remove/reload).
What **does** change: nine specific singleton assumptions in `__init__.py`,
`api.py`, `state.py`, `chart_log.py`, `learning.py`, and `config_flow.py` get
replaced with entry-scoped equivalents, plus one independent carried-over
bugfix (PR10) in `automation.py`/`coordinator.py`.

## Carried-Over Citations

These citations are cited here where relevant to this document; PR10 (the
indoor-temp-read fix) is the only piece of new work they justify — the
`zones` dict framing they were originally drawn from is not part of this
document.

- `AutomationEngine._get_indoor_temp_f()` (`automation.py:9691-9713`) is a second,
  independent "read indoor temp" implementation parallel to
  `coordinator._get_indoor_temp()` (`coordinator.py:3002-3050`), missing the
  plausibility guard (`_MIN/_MAX_PLAUSIBLE_INDOOR_F`, `coordinator.py:3014,3039`)
  that the coordinator's version has. 13 call sites in `automation.py`: 3395,
  3424, 3722, 3803, 3812, 3975, 5870, 7037, 7626, 7671, 7891, 7933, 8894.
  Confirmed live, present-tense, exploitable bug — a bad sensor reading (e.g.
  999°F) flows unguarded into real HVAC decisions via the 5-min backstop timer
  `_thermo_backstop_task()`/`async_call_later` at `automation.py:8890`, and the
  door/window listener `handle_door_window_open()` at `automation.py:3371`.
  **Fix (PR10):** extract shared read logic into one stateless helper module,
  following the existing `fan_status.py::resolve_untracked_fan_status()`
  precedent for this exact shape of cross-file duplication fix — not a mixin,
  since `ClimateAdvisorCoordinator` and `AutomationEngine` are composed, not
  related by inheritance, elsewhere in the codebase. Both classes become thin
  callers. Preserve the cadence difference: `automation.py`'s timers/listeners
  need fresh reads between coordinator cycles — do not route
  `AutomationEngine`'s reads through a coordinator-passed parameter, which
  would starve these paths of live data.
- `_build_predicted_indoor_future()` is DEFINED at `coordinator.py:9179-9188` (not
  a call site); real call sites are `coordinator.py:2384-2395`, `3423` (both in
  already-async contexts), and `8224` (inside `get_chart_data()`,
  `coordinator.py:8099`, whose only caller `api.py:343-345` already wraps it in
  `await hass.async_add_executor_job(...)` — confirmed thread-safe, no gap).
  Cited here only for context; no zone-work dependency.
- `door_window_sensors` (`config_flow.py:414-419` setup, `928-936` options) —
  existing multi-entity `EntitySelector(multiple=True)` list precedent, cited
  only for contrast where relevant — not reused as the zone model (a zone is a
  config entry, not a list entry).
- `api.py:542` — existing redaction pattern (`if key == "notify_service" or
  meta.get("sensitive")`), cited above under [Redaction](#redaction-resolved-by-existing-precedent-no-owner-decision-needed).

## Prerequisites for Implementation

1. PR3's empirical spike (see [Implementation Sequence](#implementation-sequence)) must run against a
   real/dev HA instance and its result documented before PR5 is designed in
   detail.
2. PR1 through PR10 follow the sequencing above; PR4 ships as soon as PR2's
   harness can validate it, because it is the safety-critical fix (Gap 5's
   destructive-action misdirection) and does not depend on PR3's result. PR5
   (panel/view registration) is gated on PR3.
3. **PR8 must implement zone naming via `entry.title` specifically** (per
   [Gap 7](#gap-7--no-zone-naming-field-exists)) — not a placeholder string with
   no `hass.config_entries.async_get_entry()` accessor — so the future
   zone-influence selector does not need its own new read path.
4. This document must be reviewed by Verification and signed off by the project
   owner. **This document alone does not authorize implementation.**

## Code Reference

- [`async_setup_entry`](../custom_components/climate_advisor/__init__.py#L411) — per-entry coordinator construction (already correct); setup ordering relevant to Gap 6
- [`handle_reset_learning_data`](../custom_components/climate_advisor/__init__.py#L503) — Gap 5's confirmed closure-capture bug
- [service registrations](../custom_components/climate_advisor/__init__.py#L451) — `respond_to_suggestion`, `force_reclassify`, `resend_briefing`, `dump_diagnostics`
- [`reset_learning_data` registration](../custom_components/climate_advisor/__init__.py#L509) — domain-scoped, overwritten by second entry's setup
- [REST view registration](../custom_components/climate_advisor/__init__.py#L517) — Gap 6
- [panel registration](../custom_components/climate_advisor/__init__.py#L521) — `async_register_built_in_panel`, fixed `frontend_url_path`, Gap 6
- [`_get_coordinator`](../custom_components/climate_advisor/api.py#L72) — Gap 4, first-entry selection, 21 call sites
- [`api.py` sensitive-field redaction](../custom_components/climate_advisor/api.py#L542) — existing precedent reused for the redaction question
- [`LearningEngine.__init__`](../custom_components/climate_advisor/learning.py#L678) — Gap 1, fixed-filename storage path
- [`LEARNING_DB_FILE`](../custom_components/climate_advisor/const.py#L278) — Gap 1's fixed filename constant
- [`STATE_FILE`](../custom_components/climate_advisor/const.py#L266) — Gap 2's fixed filename constant
- `StatePersistence` (`state.py`) — Gap 2
- `ChartStateLog` / `_CHART_LOG_FILE` (`chart_log.py`) — Gap 3
- [`async_create_entry` title hardcode](../custom_components/climate_advisor/config_flow.py#L559) — Gap 7, no zone-naming field
- [`AutomationEngine._get_indoor_temp_f`](../custom_components/climate_advisor/automation.py#L9691) — PR10, carried-over duplicate-implementation bug
- [`coordinator._get_indoor_temp`](../custom_components/climate_advisor/coordinator.py#L3002) — PR10, the correct version with the plausibility guard
- [`fan_status.py::resolve_untracked_fan_status`](../custom_components/climate_advisor/fan_status.py) — precedent pattern for PR10's extraction and for the module-vs-mixin decision
- [`coordinator.py:464`](../custom_components/climate_advisor/coordinator.py#L464) — existing per-entry `LearningEngine` construction that Gap 1's fix unlocks for free
- [`coordinator.py:476-486`](../custom_components/climate_advisor/coordinator.py#L476) — existing per-entry `AutomationEngine` construction, already correct
- `zone_registry.py` (new) — Gap 4's fix; `get_coordinator`/`iter_coordinators`/`get_default_coordinator`, also the future Zone Influence feature's accessor surface
- `storage_paths.py` (new) — Gaps 1-3's shared fix; `resolve_entry_scoped_path()`, single source of truth for entry-scoped filenames
- [`api.py:744`](../custom_components/climate_advisor/api.py#L744) — `request.query.get("hours", 12)`, the existing query-param precedent Gap 4's `entry_id` parameter follows
- [`coordinator.py:1495`](../custom_components/climate_advisor/coordinator.py#L1495) — `hass.config_entries.async_get_entry(self._entry_id)`, the `entry.title` read precedent Gap 7's fix must use
- [`coordinator.py:450`](../custom_components/climate_advisor/coordinator.py#L450) — `self._entry_id` attribute, read by the `entry.title` accessor above
- [`repairs.py:38,77`](../custom_components/climate_advisor/repairs.py#L38) — `hass.config_entries.async_entries(DOMAIN)` precedent for the Transitional Safety Window's deterministic fallback order
- [`const.py:1058-1059`](../custom_components/climate_advisor/const.py#L1058) — `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` (1058, 300s) / `THERMAL_FAN_SAMPLE_INTERVAL_S` (1059, 120s), cited in Zone Influence's data-flow-cadence analysis
- [`door_window_fsm.py:310-311`](../custom_components/climate_advisor/door_window_fsm.py#L310) — `DoorWindowFsmInputs.natural_vent_active`/`.whf_owns_hvac`, the communicating-automata precedent for the future Zone Influence FSM's cross-instance `Inputs` shape
- `build_coordinator.py:181` (`tools/sim_harness/build_coordinator.py`) — `build_headless_coordinator()`, constructs `ClimateAdvisorCoordinator` directly, bypassing `async_setup_entry()` entirely; the reason Gaps 5/6/8/9 have no regression coverage today
- `build_coordinator.py:177-180` — comment confirming `__init__` never touches `async_track_*`/`hass.bus`, i.e. no setup-entry side effects occur via the direct-construction path
- `ha_stubs.py:179-229,317-319` (`tools/sim_harness/ha_stubs.py`) — existing `_MockConfigFlow`/`_MockOptionsFlow` realification precedent PR2's new config-entry stub follows
- `fake_hass.py` (`tools/sim_harness/fake_hass.py`) — confirmed no `.data`/`.config_entries` attributes exist today; both required for PR2
- `tests/test_api.py:43-60` — existing hand-built `hass.data = {DOMAIN: {"entry_1": coord}}` `MagicMock()` precedent for the shape `build_headless_multi_zone()` should produce via the real setup path instead
- [`dump_diagnostics` fields](../custom_components/climate_advisor/__init__.py#L474) (`__init__.py:474-488`) — existing log-only diagnostics payload, superseded (not replaced) by the new `diagnostics.py` hook
- `diagnostics.py` (new, does not exist yet) — `async_get_config_entry_diagnostics()`, HA's native Download Diagnostics hook; see [Diagnostics and Field Feedback](#diagnostics-and-field-feedback)
- `docs/HA-BOUNDARY-EXCEPTIONS.md` — one active exception (learning-DB file); no new entry needed for `diagnostics.py`
- [HA Developer Docs — Implements diagnostics](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics/) — official signature/pattern for `async_get_config_entry_diagnostics`, `TO_REDACT`, and `async_redact_data()`
- [HA Developer Docs — Config Flow](https://developers.home-assistant.io/docs/core/integration/config_flow/) — config-entry title/naming guidance. `title_placeholders` governs the flow's own display title during setup/discovery, not a config entry's persisted `title`; Gap 7's actual fix is the existing `async_create_entry(title=...)` call site (`config_flow.py:559`) taking a real value instead of the hardcoded string — a mechanism this codebase already uses. The page does not cover cross-entry selection or repeatable-item options-flow patterns, so the Zone Influence config-surface sketch and PR8's naming flow both remain this document's own design, not a documented HA pattern.
- Home Assistant developer-docs and community search results on `notify.persistent_notification` — confirms it is a fixed built-in notify target, not a user-configured notification service; the basis for keeping the Transitional Safety Window's mechanism on `issue_registry`/`repairs.py` rather than a notification call.
