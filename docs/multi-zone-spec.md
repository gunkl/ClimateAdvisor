<!-- Nav: ← [docs/00-PROJECT-INSTRUCTIONS.md] | → [__init__.py#L480 | api.py#L73 | learning.py#L679 | state.py#L28 | chart_log.py#L46 | config_flow.py#L602 | indoor_temp.py#L44] | ↔ [docs/02-ARCHITECTURE-REFERENCE.md] -->

# Multi-Zone Support — Territory Spec (Tier 3)

> **STATUS: All nine gaps closed; every step built and tested. Awaiting
> branch-landing (not yet merged to `main`).**
>
> **Phase A / Phase B / Phase C / Phase D implementation note (Issue #796,
> uncommitted on `feature/796-multi-zone-support`):** Phase D (Step 9, the
> dashboard zone selector, PR9) is now built — the last step this document
> tracked as unstarted. All ten PRs (PR1, PR2, PR4, PR5, PR6, PR7, PR8, PR9,
> PR10) are built, linted clean, and passing the full test suite (5139
> tests) plus all 91 golden scenarios plus 27/28 Playwright UI tests (1
> pre-existing failure unrelated to this branch, confirmed via git-stash
> comparison). **PR3 (the empirical Gap 6 spike) remains the one deliberate,
> permanent exception** — see the "(as built, PR5)" note under
> [Gap 6](#gap-6--panelview-registration-needs-empirical-verification) for
> how PR5 closes Gap 6's safety concern without needing PR3's answer, and
> [Open Questions](#open-questions-carried-forward-out-of-this-build) below
> for what remains genuinely open at branch-landing time. This is an
> intentional, documented gap carried forward to production verification —
> not an unstarted step. Deviations between this section's original design
> and what Phase A/B/C/D actually built are called out inline below, each
> marked **(as built)**.
>
> **Known doc debt (pre-existing, not introduced by Phase A, C, or D):** a
> Verification pass found stale `file.py:NNN` citations scattered outside the
> areas Phase A/C/D touched — thermal-constant citations (`const.py`, off by
> ~25 lines), `door_window_sensors` config_flow citations (off by ~30-50
> lines), and the `_build_predicted_indoor_future`/`get_chart_data`
> carried-over citations in `coordinator.py` (off by ~370-390 lines). The Gap
> 8 unload-cluster citations that were previously off by ~1 line were
> resolved as a side effect of Phase C's own line-range refresh in
> `__init__.py` (Phase C added code directly above `async_unload_entry()`,
> which this pass re-cited anyway). None of the remaining ones are in
> files/functions Phase A's 5 steps, Phase C's Step 7, or Phase D's Step 9
> modified, so they predate this branch and stay out of scope for this
> document's Verification gate. Sweep these in a dedicated citation-refresh
> pass before the branch lands — see
> [Open Questions](#open-questions-carried-forward-out-of-this-build).
>
> **→ [Open Questions carried forward out of this build](#open-questions-carried-forward-out-of-this-build)**
> — everything still genuinely unresolved at the end of Phase D, in one
> place, for whoever lands this branch.

## Open Questions carried forward out of this build

Everything below is genuinely unresolved as of Phase D's close (Step 9,
2026-09-01) — not a restatement of finished work, and not a new finding
introduced by this Scribe pass. Each item cross-references the section that
already tracks it in detail; this section exists so a reader (or the
Coordinator/owner) doesn't have to search the whole document to find them.

1. **PR3's empirical spike was never run against production.** Tracked in
   detail at [Implementation Sequence, PR3](#implementation-sequence) and
   [Relationship to PR3's manual spike](#relationship-to-pr3s-manual-spike).
   Phase B shipped PR5 designed against the worst-case assumed outcome
   rather than a confirmed one. **Open validation item:** confirm the
   assumption against a real HA instance (dev or production, owner's
   discretion) before or shortly after this branch ships.
2. ~~Whether `apiFetchStream` should be zone-scoped~~ — **fixed during
   Verification, no longer open.** `ClimateAdvisorInvestigateView.post()`
   (`api.py`) already calls `_get_coordinator(hass, request)` — Phase C's
   backend work already supported an `entry_id` query param on this
   endpoint, PR9 just never wired the frontend call to send one.
   `apiFetchStream()` now calls `_withZoneParam(path)` as its first line,
   identically to how `apiFetch()` uses it (`index.html`). An AI report now
   generates for, and renders under, the zone actually selected in the
   dashboard rather than silently falling back to
   `zone_registry.get_default_coordinator()`.
3. **PR9's manual dashboard verification was Playwright + a mock-server
   harness — real headless Chromium, but not real Home Assistant.** The
   4-test `tests/ui/zone-selector.spec.js` suite (see the "(as built, PR9)"
   note under [Gap 4](#gap-4--apipy-first-entry-selection-entire-rest-surface)
   for exactly what it covers) drives a real browser against mocked
   `/api/climate_advisor/*` responses, which validates the frontend's own
   rendering/re-fetch logic thoroughly. It does **not** exercise: HA's real
   panel-registration path (the actual `async_register_built_in_panel`
   mechanics PR3's spike was meant to observe), or live multi-zone REST
   behavior against a real multi-entry HA install. Auth specifically is only
   partially exercised: `tests/ui/mock-server.js` (`mock-server.js:78-81`)
   injects a `localStorage` `hassTokens` value before the page loads so
   `getAuthToken()`'s **fallback** path (`index.html:824-830`) resolves a
   token and `initLoad()` fires — but `getAuthToken()`'s **primary** path
   (`_getHassAuth()`, `index.html:805-810`, reading `window.parent.document
   .querySelector('home-assistant').hass.auth`) is never reached in this
   harness, since the mock server serves the page standalone with no HA
   parent iframe. The real HA panel-embedding case — where `_getHassAuth()`
   succeeds and drives token refresh — remains unverified against real HA
   and is a natural first thing to check once this branch reaches a dev or
   production instance.
4. **Known test-infra gap from Phase C:** `homeassistant.util.dt`
   parent-attribute shadowing in `tools/sim_harness/ha_stubs.py` — see
   [Known test-infra gap (as built, PR7)](#known-test-infra-gap-as-built-pr7-homeassistantutildt-parent-attribute-shadowing)
   for the full description. Status remains **open**; Phase D did not touch
   `ha_stubs.py` and did not need to for Step 9's scope.
5. **Pre-existing doc-citation debt from Phase A's Verification pass** —
   see the "Known doc debt" paragraph in this STATUS callout, above, for the
   specific files/line-ranges affected. Not re-enumerated here; referenced
   so it isn't lost when this branch lands.

None of the above blocks merging this branch — each is either a deliberate,
documented design deferral (items 1, 2) or a verification/tooling gap that
existed before Phase D and is orthogonal to Step 9's own scope (items 3-5).
They are listed together so landing this branch doesn't quietly drop them.

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
| Is per-zone learning a separate feature to design? | No — it falls out for free once Gap 1 (entry-scoped `LearningEngine`) is fixed; `coordinator.py:483` already constructs one per entry. | [Resolved Questions](#resolved-questions) |
| What ships first? | The diagnostics hook and the test harness — both have zero dependencies and everything else benefits from having them in place first. | [Implementation Sequence](#implementation-sequence) |
| How does Gap 4's fix resolve "which zone" without inventing a second mechanism later? | A new `zone_registry.py` module (`get_coordinator`/`iter_coordinators`/`get_default_coordinator`) serves both the dashboard/API need now and the future cross-zone-read need. | [Gap 4](#gap-4--apipy-first-entry-selection-entire-rest-surface) |
| Is a later "zones affect each other" feature blocked by any in-scope fix? | No — `zone_registry.py`, Gap 7's `entry.title` requirement, and `storage_paths.py` all support it without modification. | [Future: Zone Influence](#future-zone-influence-deferred-not-in-scope-for-implementation) |
| How do I test this without real multi-zone hardware? | The current harness bypasses `async_setup_entry()` entirely, so Gaps 5/6/8/9's fixes have no automated regression test. A harness extension drives the real setup/unload path with two config entries. | [Testing Without Multi-Zone Hardware](#testing-without-multi-zone-hardware) |
| How do I get fast feedback from real multi-zone users? | A native HA `diagnostics.py` hook replaces the log-only `dump_diagnostics` service with a one-click downloadable bundle carrying multi-zone-specific fields, plus a symptom-to-gap triage checklist. | [Diagnostics and Field Feedback](#diagnostics-and-field-feedback) |
| What does each user-visible change actually look like? | Five mocked surfaces (naming field, entry list, Repairs card, diagnostics menu item, dashboard selector); mocking them surfaced two real refinements (conditional selector rendering, explicit Repairs card copy). | [UI Mocks](#ui-mocks) |
| What changes for a user, in plain terms? | A before/after table across eight areas, each tied to the design choice behind it. | [Outcomes: Before and After](#outcomes-before-and-after) |
| What's still open now that every step is built? | PR3's spike, real-HA verification of PR9, a known test-harness gap, and pre-existing citation debt — none blocking, all tracked in one place. (`apiFetchStream`'s zone-scoping was closed during Verification, no longer open.) | [Open Questions](#open-questions-carried-forward-out-of-this-build) |
| Were all zone-context gaps caught by the original nine-gap review? | No — Issue #812's audit found four more (dashboard first-load, Repairs `entries[0]`, zero log attribution, non-deterministic registry fallback order), all fixed. `api.py`'s own logging remains unscoped (flagged, not fixed). | [Gap 10](#gap-10--residual-zone-context-gaps-found-by-issue-812s-audit) |
| Does every zone send its own copy of the daily briefing notification? | It used to — Issue #817 Part 3/4 added a per-zone mute (`CONF_BRIEFING_NOTIFICATIONS_ENABLED`), defaulting only the stably-first zone to notifying, plus made the dashboard's Regenerate button stop force-sending a real push/email. | [Gap 10e](#10e--every-zone-independently-sent-its-own-daily-briefing-notification-issue-817-part-34) |

## Scope

Which code section this spec covers.

- **Files:**
  - `custom_components/climate_advisor/__init__.py` — entry setup, service/view/panel registration (Gaps 5/6/8/9, PR4/PR5, **DONE**; the `handle_dump_diagnostics` redirect is PR1, **DONE**; Gap 4's Transitional Safety Window Repairs issue is PR7, **DONE**)
  - `custom_components/climate_advisor/api.py` — REST surface, coordinator resolution (Gap 4, PR7, **DONE**)
  - `custom_components/climate_advisor/state.py` — `StatePersistence` (Gap 2, PR6, **DONE**)
  - `custom_components/climate_advisor/chart_log.py` — `ChartStateLog` (Gap 3, PR6, **DONE**)
  - `custom_components/climate_advisor/learning.py` — `LearningEngine` (Gap 1, PR6, **DONE**)
  - `custom_components/climate_advisor/config_flow.py` — entry creation, zone naming (Gap 7, PR8, **DONE**)
  - `custom_components/climate_advisor/automation.py` / `coordinator.py` / `indoor_temp.py` (new) — carried-over indoor-temp-read duplication, now fixed via the shared `indoor_temp.py` module (independent track, PR10, **DONE**, see [Carried-Over Citations](#carried-over-citations))
  - `custom_components/climate_advisor/zone_registry.py` (Gap 4, PR7, **DONE**; `list_zones()` added PR9, **DONE**) — also the accessor surface a future Zone Influence feature would use
  - `custom_components/climate_advisor/storage_paths.py` (new, Gaps 1-3, PR6, **DONE**)
  - `custom_components/climate_advisor/diagnostics.py` (new, PR1, **DONE**) — native HA diagnostics hook, see [Diagnostics and Field Feedback](#diagnostics-and-field-feedback)
  - `custom_components/climate_advisor/frontend/index.html` — dashboard zone selector (`.zone-tabs`/`.zone-tab-btn`, `_selectedEntryId`, `apiFetch()` parameterization; PR9, **DONE**)
  - `tools/sim_harness/ha_stubs.py`, `tools/sim_harness/fake_hass.py`, `tools/sim_harness/build_coordinator.py` (extended, harness-only — no production code), `tools/sim_harness/multi_zone_assertions.py` (new, harness-only) — see [Testing Without Multi-Zone Hardware](#testing-without-multi-zone-hardware) (PR2, **DONE**)

  **Phase A/B/C/D status (see the STATUS callout at the top of this
  document):** all ten PRs (PR1, PR2, PR4, PR5, PR6, PR7, PR8, PR9, PR10)
  are built, uncommitted on `feature/796-multi-zone-support`. PR3
  (empirical spike) is a deliberate non-run — see Gap 6's "(as built, PR5)"
  note for why it's no longer load-bearing, and
  [Open Questions](#open-questions-carried-forward-out-of-this-build) for
  what that leaves genuinely open. PR9 (the dashboard zone selector) is
  now built — see the "(as built, PR9)" note under
  [Gap 4](#gap-4--apipy-first-entry-selection-entire-rest-surface).
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
# __init__.py:480 (was :420 pre-Phase-B — PR4/PR5 added ~150 lines above this
# point for zone-scoped service/panel registration guards; the line itself
# is unchanged)
coordinator = ClimateAdvisorCoordinator(hass, dict(entry.data), entry_id=entry.entry_id)
```

Each coordinator already constructs its own `AutomationEngine` bound to that
entry's `climate_entity` (`coordinator.py:495-505`), with no shared/global mutable
state between instances. `LearningEngine` is likewise constructed per coordinator
(`coordinator.py:483`). Nothing in `manifest.json` or `config_flow.py`'s
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

**Update (Issue #812):** this section's original review found nine gaps.
A tenth, [Gap 10](#gap-10--residual-zone-context-gaps-found-by-issue-812s-audit)
(itself bundling four independent findings — dashboard first-load, Repairs
targeting, log attribution, registry fallback order), was found later by a
follow-up audit and is documented at the end of this list, after Gap 9. The
heading and framing below are kept as originally written (nine) rather than
silently renumbered, since Gaps 1-9 are still exactly the original nine —
Gap 10 is additive, not a renumbering of the set.

#### Gap 1 — `LearningEngine` DB collision

**Fixed, PR6 DONE (Phase A)** — see the "(as built, PR6)" note under
[Fix design](#gap-3--chartstatelog-collision-same-bug-third-file) below. The
paragraph immediately below describes the pre-fix bug.

`LearningEngine.__init__` (pre-fix: `learning.py:678`, now `learning.py:679`)
took only `storage_path`, and wrote
to a fixed filename `LEARNING_DB_FILE = "climate_advisor_learning.json"`
(`const.py:292`) under `hass.config.config_dir`. Two entries would collide — the
second entry's learning writes clobbering the first's, or vice versa depending
on save timing.

#### Gap 2 — `StatePersistence` collision (same bug, second file)

`state.py`'s `StatePersistence` writes to a fixed filename `STATE_FILE =
"climate_advisor_state.json"` (`const.py:280`), with no entry-scoping. Same
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
  Each of `LearningEngine.__init__` (`learning.py:679`), `StatePersistence.__init__`
  (`state.py:28`), `ChartStateLog.__init__` (`chart_log.py:46`) calls this
  once instead of hand-rolling its own path join. Verified safe against all
  three actual filenames (`STATE_FILE = "climate_advisor_state.json"`,
  `LEARNING_DB_FILE = "climate_advisor_learning.json"`, `_CHART_LOG_FILE =
  "climate_advisor_chart_log.json"` — `const.py:280,292`, `chart_log.py:26`) —
  each has exactly one `.`, so `rsplit(".", 1)` splits correctly.

  This is a **module, not a mixin** — same decision as
  [Shared-extraction module vs. mixin](#shared-extraction-module-vs-mixin-module-decided-not-an-open-question)
  below, applied to a second occurrence of the identical duplication shape.

  **(as built, PR6):** the shipped `resolve_entry_scoped_path()`
  (`storage_paths.py:29-56`) deviates from the snippet above in one respect —
  when `entry_id` is falsy (empty string), it returns the plain unscoped
  `config_dir / base_filename` instead of unconditionally appending
  `_{entry_id}`. Two real callers depend on this: (1) the simulation harness
  and ~90 existing unit tests construct `StatePersistence`/`ChartStateLog`/
  `LearningEngine` directly with no `entry_id`, asserting against the literal
  unscoped filename as part of testing unrelated behavior (atomic-write,
  corruption-recovery, tmp-file cleanup) — always-scoping would silently
  rename their target file out from under them; (2)
  `ClimateAdvisorCoordinator.__init__` already treats `entry_id=""` as its own
  established "no resolvable config entry" case, so treating `""` as "use the
  legacy unscoped path" here is consistent with that existing meaning rather
  than inventing a new one. Production always passes a real, non-empty
  `entry_id`, so this fallback is a test/harness accommodation, not a
  production behavior change from the design above.

  Also shipped but not in the original design snippet: `storage_paths.py`
  adds `migrate_legacy_storage_file(config_dir, base_filename, entry_id)`
  (`storage_paths.py:59-154`) — the one-time, idempotent migration this
  document's [Pre-conditions](#pre-conditions) item 3 and
  [Implementation Sequence](#implementation-sequence) PR6 already called for.
  It no-ops when `entry_id` is falsy, when the entry-scoped file already
  exists, or when no legacy file exists; otherwise it copies the legacy file
  to the entry-scoped path via the existing write-tmp-then-`os.replace`
  pattern (`state.py`/`chart_log.py`'s own atomic-write precedent) and only
  then unlinks the legacy file, so a crash mid-migration always leaves at
  least one readable copy. Each of `StatePersistence.load()`
  (`state.py:35`), `ChartStateLog.load()` (`chart_log.py:61`), and
  `LearningEngine.load_state()` (`learning.py:701`) calls it before checking
  whether its (now-resolved) path exists.

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
  (`hass.data[DOMAIN]`, confirmed `__init__.py:496` (was `:431` pre-Phase-B,
  `:491` pre-Phase-C — Phase B's PR4/PR5 additions and Phase C's
  `_resolve_zone_coordinator()` docstring growth both sit above this line,
  not the line itself):
  `hass.data[DOMAIN][entry.entry_id] = coordinator`), one accessor surface
  designed for both consumers instead of two mechanisms built at different times.
- **How `entry_id` reaches the 21 `api.py` call sites**: a query parameter
  (`request.query.get("entry_id")`), not a URL path segment. `api.py:758`
  (was `:744` pre-PR7 — PR7's `zone_registry` import and `_get_coordinator()`
  docstring/signature growth sit above this line) already uses a bare
  `request.query` accessor (`hours =
  float(request.query.get("hours", 12))`) — the other existing request-scoped
  parameters in this file (lines 301, 343, 348, 954 — were 287, 329, 334, 940
  pre-PR7, a consistent +14-line shift from PR7's additions above them) go
  through `request.rel_url.query` instead; `entry_id` can follow either
  precedent.
  `request.match_info` (the path-segment
  mechanism) has **zero** uses anywhere in `api.py` — a path-segment approach
  would require restructuring all 21 routes' URL patterns and `aiohttp`
  registration for the same outcome, a much larger diff that also isn't needed
  for Gap 4's actual bug (first-entry selection, not resource-identity
  modeling). Concretely: each handler adds `entry_id =
  request.query.get("entry_id")` at the top, passes it to
  `get_coordinator`/`get_default_coordinator`.

**(as built, PR7 — Step 7):** fixed, matching the design above closely.
Confirmed by reading `zone_registry.py` and `api.py`:

- **`zone_registry.py`'s three functions ship exactly as designed** —
  `get_coordinator(hass, entry_id)`, `iter_coordinators(hass)`, and
  `get_default_coordinator(hass)` (`zone_registry.py:95,100,105` — shifted
  from `:39,44,49` by the Verification throttle fix's `_warn_once()`/
  `reset_warning_state()` additions above them in the file), each a plain
  module-level function reading `hass.data.get(DOMAIN, {})`, no HA
  subclassing, matching the `fan_status.py` precedent this design cited.
- **`get_default_coordinator()`'s zero/one-zone path is a true fast path,
  not just a conceptual one:** when `len(entries) == 1`, it returns
  `next(iter(entries.values()))` directly and never calls
  `hass.config_entries.async_entries()` at all (`zone_registry.py:132-133`) —
  confirmed by
  `tests/test_zone_registry.py::TestGetDefaultCoordinatorZeroOrOneZone::test_single_zone_path_does_not_consult_config_entries`,
  which asserts `hass.config_entries.async_entries.assert_not_called()`. The
  multi-zone fallback (`zone_registry.py:135-182`) walks
  `hass.config_entries.async_entries(DOMAIN)`'s stable order (not dict
  order — proven independently of dict order by
  `test_fallback_follows_config_entries_order_reversed`), returns the first
  entry that also has a loaded coordinator in `hass.data[DOMAIN]` (skipping
  any config entry that's mid-setup and not yet present in `hass.data`), and
  logs a WARNING naming the entry_id it picked — throttled (see "WARNING
  throttle fix" below) rather than emitted on every call. A defensive final
  fallback (`next(iter(entries.values()))`) covers the case where
  `async_entries()` itself returns nothing despite `hass.data[DOMAIN]` being
  non-empty — not expected in practice, and now also logged (throttled the
  same way), not silent.
- **`api.py`'s `_get_coordinator()` (`api.py:73-91`) takes the request as a
  second parameter**, not just `hass` as the pre-fix design snippet showed —
  reads `entry_id = request.query.get("entry_id")` (bare `request.query`,
  matching the `hours` param's existing precedent, not
  `request.rel_url.query`) and, when present, resolves via
  `zone_registry.get_coordinator()`; when absent, falls back to
  `zone_registry.get_default_coordinator()`. All **21** of the file's
  `HomeAssistantView` subclasses were updated to call `_get_coordinator(hass,
  request)` instead of the old `_get_coordinator(hass)` — confirmed via
  `git diff --stat` (`api.py` +68/-33 lines) and by counting call sites;
  `tests/test_api_multi_zone.py` drives a representative sample of 4 of the
  21 view classes (2 GET, 2 POST) end-to-end through real
  `entry_id`-resolution and the absent-`entry_id` backward-compat path,
  rather than re-testing all 21 (the remaining 18 differ only in what they do
  with the already-resolved coordinator, which their own pre-existing
  per-view tests already cover).
- **Test-infrastructure fallout, not a behavior change:** 5 pre-existing test
  files (`test_api.py`, `test_coordinator_health.py`,
  `test_api_respond_suggestion.py`, `test_ai_investigator.py`) needed
  `request.query = {}` added to their mock request builders. A bare
  `MagicMock()`'s `.query.get(...)` auto-mock return value is a truthy
  `MagicMock`, not `None` — which would have made `_get_coordinator()` take
  the `if entry_id:` branch with a garbage `entry_id` instead of falling
  through to `get_default_coordinator()`, breaking every one of these
  pre-existing single-zone view tests. This is scaffolding to keep old tests
  correct under the new signature, not new production behavior.
- **DRY follow-through into `_resolve_zone_coordinator()`:** the Gap 5 fix's
  `_resolve_zone_coordinator(hass, call)` (`__init__.py:392-423`), which
  previously hand-rolled `hass.data.get(DOMAIN, {}).get(entry_id)` inline,
  now calls `zone_registry.get_coordinator(hass, entry_id)` — one canonical
  "look up a coordinator by entry_id" implementation instead of two
  independently-maintained one-liners that could silently drift apart.

**(as built, PR9 — Step 9, dashboard zone selector):** ships PR9, the last
step the Implementation Sequence listed as unstarted. Confirmed by reading
`zone_registry.py`, `api.py`, and `frontend/index.html`'s diffs directly:

- **New `zone_registry.list_zones(hass) -> list[dict[str, str]]`**
  (`zone_registry.py:105-125`), not one of the three functions this Gap
  originally specified — those three (`get_coordinator`/`iter_coordinators`/
  `get_default_coordinator`) resolve ONE zone's coordinator; the dashboard
  selector needs to enumerate ALL loaded zones with a human-readable label,
  which none of the three do. Returns `[{"entry_id": ..., "title": ...}]` in
  the stable order of `hass.config_entries.async_entries(DOMAIN)`, filtered
  to entries also present in `hass.data[DOMAIN]` — the same stable-order
  precedent already cited for the Transitional Safety Window fallback
  (`repairs.py:38,77`), not dict-iteration order, and the same
  loaded-coordinator scope `iter_coordinators()` already uses.
- **Routed onto `ClimateAdvisorStatusView`, not `ClimateAdvisorConfigView`
  (a real routing decision, not the only option):** the design's original
  "How `entry_id` reaches the 21 call sites" discussion assumed the fix was
  purely about resolving a zone from a caller-supplied `entry_id`; it never
  specified which existing endpoint should hand the frontend the list of
  zones to choose from in the first place. The in-code comment on
  `api.py`'s `_status_payload["zones"]`/`"zone_count"` additions gives the
  reason: `loadStatus()` polls every 60s regardless of which dashboard tab
  is active, so `ClimateAdvisorStatusView` is the one endpoint guaranteed to
  have fresh data by the time the page needs to decide whether to render the
  selector row at all — `ClimateAdvisorConfigView` is fetched once per
  Settings-tab load, not on a cycle, and would leave the selector unable to
  detect a zone added or removed without a manual tab switch. `zone_count`
  is deliberately the same `len(list_zones(hass))` computation already used
  by the Transitional Safety Window Repairs check (Gap 4, above) — read
  once, reused for both, not a second parallel counting implementation.
- **`.zone-tabs`/`.zone-tab-btn` CSS, deliberately distinct from
  `.tabs`/`.tab-btn`:** `frontend/index.html` already has a `.tabs`/
  `.tab-btn` pair that switches page SECTIONS (Status/Analysis/Settings/
  Debug). The new selector switches ZONES — a different axis entirely, and
  conflating the two class names would let a future edit to one switcher's
  click-handler wiring silently affect the other. The in-code comment on the
  new CSS block states this explicitly. Visually a lighter pill-button
  variant of the same look, but a separate class family with its own
  click-handler wire-up (`renderZoneSelector()`), matching this document's
  existing Status Card Ontology precedent of never letting one UI mechanism
  answer a question that belongs to a different one.
- **`_selectedEntryId` + `apiFetch()` parameterization covers all 21 zone-data
  call sites** (updated during Verification — previously stated as "19 of
  21, with 2 deliberate exclusions"; both exclusions turned out to be real
  gaps, not deliberate scope decisions, and are now fixed): a module-level
  `_selectedEntryId` (null until a multi-zone install's selector sets it) is
  appended by a new `_withZoneParam(path)` helper called once, inside
  `apiFetch()` itself — every one of `apiFetch`'s and `apiPost`'s 19 existing
  call sites becomes zone-aware for free, rather than editing each call site
  individually. Two more call sites bypass `apiFetch()` with their own raw
  `fetch()` and needed `_withZoneParam()` applied directly at their call
  site:
  - `apiFetchStream('ai_investigate', ...)` (the AI-investigate streaming
    endpoint) now calls `_withZoneParam(path)` as its first line — the
    original doc text called this "explicitly undecided," but
    `ClimateAdvisorInvestigateView.post()` (`api.py`) already reads
    `entry_id` via `_get_coordinator(hass, request)`, so the backend already
    supported this and only the frontend call site was missing it.
  - The GitHub-issue-filing `fetch('/api/climate_advisor/submit_github_issue', ...)`
    call (`index.html`, `submitGithubIssue()`) was originally described here
    as "unrelated to zone data — filing an issue is not a per-zone action."
    That rationale was false: `ClimateAdvisorSubmitGithubIssueView.post()`
    reads `CONF_GITHUB_TOKEN`/`CONF_GITHUB_REPO` from `coordinator.config`,
    and `config_flow.py`'s `async_step_github_settings` is a per-config-entry
    options-flow step — each zone can configure its own GitHub token/repo.
    Filing an issue from zone B's dashboard while unparameterized would
    silently use whichever zone `get_default_coordinator()` falls back to,
    not zone B's own GitHub settings. Fixed by applying `_withZoneParam()`
    to this call's URL directly (it predates `apiPost()` and hand-rolls its
    own auth header, so it wasn't switched to `apiPost()` — that would also
    change its existing non-2xx/error-body handling, a larger change than
    this fix warrants).
  - `apiFetch`'s null-`_selectedEntryId` path (single-zone install, or a
    multi-zone install before the selector's first render) sends no
    `entry_id` query param at all, so the request shape for every existing
    single-zone install is byte-identical to before PR9 — the backend's
    `get_default_coordinator()` fallback (Gap 4, above) resolves the zone
    exactly as it already did.
- **Test coverage:** `tests/test_api_status_zones.py` (6 tests) drives the
  REAL `ClimateAdvisorStatusView.get()` (not a hand-rolled mirror of
  `list_zones()`, per this project's no-mirror-tests doctrine — see the
  module docstring) against `build_headless_multi_zone()` at 1/2/3 loaded
  zones, confirming: `zones`/`zone_count` describe the whole install, not
  just the coordinator a request's own `entry_id` resolved to; ordering
  matches `hass.config_entries.async_entries(DOMAIN)`'s stable order even
  when `hass.data[DOMAIN]`'s dict order is deliberately scrambled first
  (`test_dict_iteration_order_is_not_what_drives_the_result` — the same
  distinction Gap 4's own fallback test already proved once for
  `get_default_coordinator()`, now proved again for `list_zones()`); and
  `zone_count` drops from 3 to 2 after a real `async_unload_entry()` call.
  No Python test exercises the zero-zone case (right after a reload, before
  any entry has finished loading) — that path is covered only by the
  Playwright suite below. `tests/ui/zone-selector.spec.js` (4 tests) is a real
  headless-Chromium Playwright test against this project's existing
  mock-server harness (not a DOM-string assertion or a mirror of
  `renderZoneSelector()`'s logic) confirming: the selector row stays hidden
  for a single-zone mock, stays hidden for a zero-zone mock (e.g. right
  after a reload, before the first `/status` response lands), renders one
  button per zone for a 2-zone mock with the first zone active by default
  and clicking the second zone re-fetches `/status` carrying
  `entry_id=zone-living`, and — as of the Verification pass fix described
  above — the zone click **alone** (no range-button click needed) also
  triggers a `chart_data` request carrying the selected zone's `entry_id`,
  proving `_refreshAll()`'s zone-click handler now calls `loadChart()`
  itself rather than relying on the unrelated 5-min `_chartCycle` poll. The
  original version of this test clicked a range button to force the chart
  re-fetch, which meant it would still have passed even if this behavior had
  never been implemented — an ineffective test per this project's
  three-exercise protocol; it's been rewritten to assert the real behavior.
  No test in this file exercises a
  mid-session zone_count drop (e.g. 2 zones → 1 zone without a page reload)
  — `renderZoneSelector()`'s own code path for that case (clearing
  `_selectedEntryId` when `zones.length <= 1`) is implemented but only
  covered indirectly, not by a dedicated test.

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
dict-iteration order, which is unstable across restarts), plus a throttled
WARNING log (see "WARNING throttle fix" below — once per distinct resolved
outcome, not per call), plus a native HA Repairs issue, so a log line alone
isn't the only surface an admin could miss.

**Mechanism:** reuse `homeassistant.helpers.issue_registry` via this
codebase's own `repairs.py` module, which already implements two Repairs
flows today (`WeatherEntityRepairFlow`, `ReloadNeededRepairFlow`), raised via
`ir.async_create_issue()` (confirmed real call sites: `__init__.py:455-464`
(was `:395-404` pre-Phase-B) for `weather_entity_not_found`,
`config_flow.py:693-700` for `reload_needed`)
and cleared via `ir.async_delete_issue()` (confirmed: `__init__.py:437,452,471`
(was `:377,392,411` pre-Phase-B), `repairs.py:44,80`). Both surface in HA's own **Settings → Repairs** list,
visible regardless of whether the CA dashboard panel is open.

**Boundary Rule basis:** issue-registry writes are scoped to the calling
integration's own `DOMAIN` — Climate Advisor can only create/delete issues
under its own domain, structurally incapable of touching anything outside its
scope — and this exact mechanism already ships in this codebase with prior
owner approval (the two existing issues above).

**Trigger and clear condition:** `len(hass.data[DOMAIN]) > 1`, evaluated at
two lifecycle points:

- **On raise**: at the end of `async_setup_entry()`, after
  `hass.data[DOMAIN][entry.entry_id] = coordinator` (`__init__.py:496`, was
  `:431` pre-Phase-B, `:491` pre-Phase-C), recompute the zone count; if it's
  now `> 1`, call `ir.async_create_issue(hass, DOMAIN,
  "zone_resolution_ambiguous", is_fixable=False, is_persistent=True,
  severity=ir.IssueSeverity.WARNING,
  translation_key="zone_resolution_ambiguous")` — mirroring
  `weather_entity_not_found`'s shape, minus a guided fix flow (there's nothing
  for the user to configure).
- **On clear**: at the start of `async_unload_entry()`, after this entry is
  popped from `hass.data[DOMAIN]`, recompute the zone count; if it's now
  `<= 1`, call `ir.async_delete_issue(hass, DOMAIN, "zone_resolution_ambiguous")`.
  This is new code — `async_unload_entry()` (`__init__.py:695-743`, was
  `:545-562` pre-Phase-B, `:668-706` pre-Phase-C; the function grew again by
  this raise/clear pair itself, on top of the Gap 8/9 teardown guards
  described under those gaps' "(as built, PR4/PR5)" notes).

**(as built, PR7 — Step 7):** fixed exactly as designed, confirmed by reading
`__init__.py`. The raise block sits at `__init__.py:496-518`, immediately
after `hass.data[DOMAIN][entry.entry_id] = coordinator` and clearly commented
as its own Issue #796 block, distinct from the pre-existing service/view/panel
registration that follows it lower in the same function. The clear block sits
at `__init__.py:697-708`, immediately after
`coordinator: ClimateAdvisorCoordinator = hass.data[DOMAIN].pop(entry.entry_id)`
— and, as the design called for, it is its **own standalone `if len(hass.data[DOMAIN])
<= 1:` check**, not nested inside the pre-existing `if not hass.data[DOMAIN]:`
block a few lines below it (`__init__.py:713`) that owns the Gap 8/9
last-zone-only service/panel teardown. The in-code comment at
`__init__.py:700-706` explains why: going from 2 zones to 1 must clear this
issue immediately, even though the last-zone teardown block only fires at
exactly 0 remaining zones — a different threshold entirely. `is_fixable=False`
and no `fix_flow` block were both confirmed in the `strings.json`/
`translations/en.json` diff (only `title`/`description` keys added under
`"zone_resolution_ambiguous"`, no `"fix_flow"` sibling key, unlike
`"weather_entity_not_found"` which has one) — there is genuinely nothing to
configure, matching the design's rationale. Regression coverage:
`tests/test_repairs.py`'s new `TestZoneResolutionAmbiguousIssue` class (5
tests) drives the real `async_setup_entry()`/`async_unload_entry()` via
`build_headless_multi_zone()` rather than re-implementing the threshold
checks, covering: raised at 2 zones, not raised at 1 zone, cleared when
unloading back to 1 zone, **not** cleared when unloading 3→2 zones (proving
the `<= 1` threshold is exact, not merely "cleared whenever unloading
happens"), and a `cross_zone_isolation` check confirming the issue's
lifecycle has no bleed into a sibling zone's own coordinator state.

**WARNING throttle fix (Phase C Verification correction, post-PR7):** the
as-built description above and the "logs a WARNING" language throughout this
section originally described (and PR7 as first merged actually did) log
unconditionally on **every** `get_default_coordinator()` call. That was a
real regression, caught in Verification, not a documentation nit: with PR9
(the dashboard zone selector) not yet shipped, every dashboard `_pollCycle`
sends no `entry_id` and hits 5 endpoints every 60s, so a 2+-zone install
logged this WARNING 5×/min through this path alone — enough to fully evict
`log_capture.py`'s `LOG_CAPTURE_CAP=200`-entry ring buffer roughly every 40
seconds, silently blanking the AI Investigator's "System Errors/Warnings"
section for the entire life of any multi-zone install. Fixed by throttling:
`zone_registry.py`'s `_warn_once(hass, token, message, *args)` logs a given
message at most once per distinct `token` (the resolved outcome — e.g.
`f"ambiguous:{entry.entry_id}"`, or one of two fixed tokens for the two
previously-silent defensive fallback branches, which also gained WARNING
logging as part of this fix) for a given `hass` instance; a later call
resolving to a *different* outcome still logs, since that's new information.
Throttle state lives under a `hass.data` key (`_WARNED_STATE_KEY`) — the same
storage idiom `log_capture.py`'s `_HASS_DATA_KEY` and `__init__.py`'s
`_PANEL_HASS_DATA_KEY` already use for per-hass-instance "already happened"
tracking, not a module-level global (which would leak across HA instances/
tests sharing one process) and not a time-based rate limiter (no live-clock
dependency, so the throttle test suite has none either).
`zone_registry.reset_warning_state(hass)` clears this state and is called
from `async_unload_entry()` in lockstep with the `zone_resolution_ambiguous`
Repairs issue's own clear condition above, so a later recurrence of the
ambiguous condition warns again from a clean slate. Test coverage:
`tests/test_zone_registry.py::TestGetDefaultCoordinatorWarningThrottle`
(repeated-same-outcome calls log once, including 500 rapid calls with no
time mocking; a changed outcome logs again; `reset_warning_state()` restores
logging and is a no-op when nothing was ever warned) plus two new tests in
`TestGetDefaultCoordinatorMultiZoneFallback` proving the two previously-silent
defensive branches now warn.

**Scope of the signal:** `get_default_coordinator()`'s fallback is a
permanent, sanctioned feature for any caller that doesn't pass `entry_id` —
not solely a shim for the window before the dashboard ships.

**Stale — do not follow (superseded by Issue #812, kept only so the
correction is legible against what it replaces):** this paragraph originally
claimed *"Once PR9 ships, the dashboard stops hitting the ambiguous path (it
sends `entry_id`), but a direct API call, a user's own script, or a
third-party tool integrating with `api.py` without `entry_id` can still hit
it, in any multi-zone install, indefinitely."* **That was wrong.** PR9's
`_selectedEntryId` starts out `null` on every fresh page load (before
`_loadStoredZone()` existed) — meaning the dashboard's own first-load
requests, every single time, hit this exact ambiguous fallback until the
first `/status` response came back and `renderZoneSelector()` set
`_selectedEntryId`. This was not a third-party-tool edge case; it was the
dashboard itself, on every reload, for every multi-zone install. Confirmed
and fixed by Issue #812 (frontend/index.html's `localStorage`-backed
`_selectedEntryId` seeding plus a stale-selection self-heal retry — see
[Gap 10](#gap-10--residual-zone-context-gaps-found-by-issue-812s-audit)
below). The corrected claim: even after Issue #812's fix, a direct API call,
a user's own script, or a third-party tool integrating with `api.py` without
`entry_id` can still hit the ambiguous path, in any multi-zone install,
indefinitely — that part of the original claim was correct, only the "dashboard
is now excluded" part was false.

This issue is an ongoing informational signal tied to zone
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

**(as built):** moot as of Step 9 — PR7 and PR9 both live on
`feature/796-multi-zone-support` and merge to `main` together in the single
batch [Release labeling](#release-labeling--what-was-actually-followed)
describes, so there is no window where PR7 is live on `main` without PR9.
The `zone_resolution_ambiguous` Repairs signal (above) remains a permanent,
sanctioned feature for any zone-unaware caller (a direct API call, a script,
a third-party tool) even after PR9 ships — it does not become dead code.
**Correction (Issue #812):** at the time this note was written, PR9's
dashboard itself was still an unlabeled zone-unaware caller on every first
page load — see the corrected "Scope of the signal" paragraph above and
[Gap 10](#gap-10--residual-zone-context-gaps-found-by-issue-812s-audit)
below. "A direct API call, a script, a third-party tool" was never a
complete list of who could hit this path; the dashboard itself belonged on
it until #812 shipped.

#### Gap 5 — service-handler misdirection (most severe)

Five HA services — `respond_to_suggestion`, `force_reclassify`, `resend_briefing`,
`dump_diagnostics`, `reset_learning_data` (registrations at `__init__.py:460-464,
495-497, 511-516`) — are registered as closures capturing the `coordinator` local
variable bound at `__init__.py:420` for that specific `async_setup_entry` call.

`handle_reset_learning_data` (`__init__.py:504-508`) is a direct closure —
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

**(as built, PR4 — Step 4):** fixed. Confirmed by reading `__init__.py`.
Occupant-facing outcome: `climate_advisor.reset_learning_data` (and the other
four zone-scoped services) now always acts on the zone the caller actually
named — a wrong-zone destructive call is no longer possible by omission,
because omitting the zone is no longer accepted at all.

- **Call-time resolution, not a setup-time closure.** All five handlers were
  rewritten to stop closing over the `coordinator` local from one specific
  `async_setup_entry()` call. Each now calls the new module-level
  `_resolve_zone_coordinator(hass, call)` at the top of its body, which looks
  up `hass.data[DOMAIN][call.data["entry_id"]]` fresh on every invocation —
  the canonical per-entry table, not a captured reference that could go
  stale or point at the wrong zone.
- **`entry_id` is a required field on all five service schemas**, not
  optional and not inferred. `RESPOND_SUGGESTION_SCHEMA` and
  `RESET_LEARNING_SCHEMA` gained `vol.Required("entry_id"): cv.string`
  alongside their existing fields; `force_reclassify`, `resend_briefing`, and
  `dump_diagnostics` (previously `vol.Schema({})`, no fields at all) now use
  a shared `ENTRY_ID_ONLY_SCHEMA = vol.Schema({vol.Required("entry_id"): cv.string})`.
  `services.yaml` mirrors this with a `config_entry` selector
  (`integration: climate_advisor`) on all five, so the UI presents a zone
  picker rather than a free-text field. `reset_learning_data`'s field
  description adds an explicit "double check this before calling — this
  service is destructive" line, matching this gap's severity.
- **Fail-closed, not fail-silent.** `_resolve_zone_coordinator()` raises
  `ServiceValidationError` — HA's standard user-facing validation-failure
  exception, renderable by the frontend/CLI as an error rather than an
  unhandled traceback — for an unknown or already-unloaded `entry_id`,
  instead of returning `None` and letting the caller silently act on nothing
  or the wrong object. This directly closes the "no error or warning" half
  of the occupant-facing consequence described above.
- **Register-once, not register-per-zone.** All five `hass.services.async_register()`
  calls are now wrapped in `if not hass.services.has_service(DOMAIN, "respond_to_suggestion"):`
  — a second-and-later zone's `async_setup_entry()` no longer re-registers
  (and therefore no longer overwrites) identical closures, since every
  closure now resolves its target dynamically instead of capturing one.
  `ZONE_SCOPED_SERVICES` (`__init__.py`, a five-element tuple of the service
  names) is the single source of truth read by both this registration guard
  and the Gap 9 teardown loop below, so the two can't drift out of sync with
  each other.
- **`handle_dump_diagnostics` nuance:** unlike the other four handlers, it
  needs a `ConfigEntry`, not just a `ClimateAdvisorCoordinator` — the shared
  `async_get_diagnostics_payload(hass, entry)` helper (`diagnostics.py`,
  PR1) takes an `entry`. So it still calls `_resolve_zone_coordinator()`
  first (for the fail-closed `entry_id` validation) and then derives the
  entry via `hass.config_entries.async_get_entry(zone_coordinator._entry_id)`
  rather than resolving `entry` directly — a coordinator-shaped resolution
  used to reach an entry-shaped object, not a second, parallel lookup path.
- **Gap 9's mirror-image teardown**, in the same PR: see the "(as built,
  PR4)" mechanics folded into
  [Gap 9](#gap-9--services-are-never-unregistered-on-unload) below, since
  Gap 9's fix is the unload-side half of this exact change.

Regression coverage: `tests/test_service_zone_scoping.py` (new) drives the
real `async_setup_entry()`/`async_unload_entry()` via
`build_headless_multi_zone()` and asserts cross-zone isolation end-to-end
(calling a service scoped to one zone, confirming a sibling zone's state is
unchanged) rather than introspecting closures — see the "(as built, PR4)"
note under [Testing Without Multi-Zone Hardware](#testing-without-multi-zone-hardware)
for why the PR2-era `service_registry_binding` closure-walk assertion type
could not be reused here.

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

**(as built, PR5 — Step 5):** fixed, without ever answering PR3's empirical
question. The shipped fix is **"register once, domain-wide, guarded — not
per-entry-unique URLs"**, not the reordering approach outcome (b) originally
called for. Confirmed by re-reading `api.py`: every one of its 21 REST view
classes (`API_VIEWS`) has a `url = API_*` class attribute bound to a fixed
`const.py` constant, not derived from `entry_id` — meaning the REST views are
**a third instance of this exact collision**, not just the two originally
named (`async_register_static_paths`'s `PANEL_URL`, `async_register_built_in_panel`'s
`PANEL_FRONTEND_PATH`). All three are now guarded by one flag,
`_PANEL_HASS_DATA_KEY` (`__init__.py`), checked at the top of the
registration block: `if not hass.data.get(_PANEL_HASS_DATA_KEY):` — mirroring
the `has_service()` guard PR4 already established for `ZONE_SCOPED_SERVICES`.
A second-and-later zone's `async_setup_entry()` never attempts the
registration at all.

**Why this closes the safety concern without PR3's answer:** outcome (b) —
the danger this section worried about — is specifically "a second zone's
setup crashes AFTER the coordinator/engine already started, with no way to
see/stop/reach that second zone." With the guard, a second-and-later zone's
setup never calls the shared registration functions in the first place, so
there is no duplicate-registration attempt for HA's frontend/http layer to
ever crash on. The reordering approach (registration before
`coordinator.async_setup()`) was designed as defense against a crash that
could still happen even with a fix; the guard approach removes the
crash-causing call itself for every zone but the first. PR3's spike remains
formally un-run and its question academically open, but it is no longer
load-bearing for Gap 6's fix.

**Consistency with the dashboard direction:** this also matches, rather than
fights, this document's own [dashboard selector design](#dashboard-a-zone-selector-over-the-existing-card-layout-not-a-new-comparisonaggregation-card) —
a SINGLE panel with an `entry_id`-driven zone-selector row, not one physical
panel per zone. Making each zone's `frontend_url_path` unique would have
produced N separate panels/URLs that PR9 would then need to collapse back
into one selector-driven panel anyway — a fix that would have had to be
partially unwound. "Register once, guarded" is the version of Gap 6's fix
that PR9 can build on directly.

**Defense-in-depth, not the primary mechanism:** the registration block is
additionally wrapped in a broad `try/except Exception`, logged at WARNING
(`"Panel registration skipped: already registered by another zone
entry_id=%s reason=%s"`) and treated as an expected no-op rather than a fatal
setup error, in case the guard's own assumption (`hass.data` accurately
reflects HA's internal panel/view registries) is ever violated — for example
by an unusual reload sequence this design didn't anticipate. The `finally`
block sets the guard flag regardless of whether the `try` succeeded or the
`except` fired, so a caught failure is not retried by a third zone either.

**REST view teardown remains a known, separate, out-of-scope gap.** Gap 9's
scope was specifically the five zone-scoped *services*; there is still no
`hass.http`-level "unregister a view" call anywhere in `async_unload_entry()`
(HA does not offer one directly). This was true before PR5 and remains true
after — not a regression PR5 introduced, and not part of Gap 6/8's scope as
originally written. The REST views themselves are stateless dispatchers that
resolve their target coordinator per-request (once PR7/Gap 4 lands), so a
lingering view registration after the last zone unloads is a dormant/inert
endpoint, not a live binding to a defunct object the way Gap 9's service
closures were — a materially smaller risk, but flagged here for whoever
scopes a future teardown-completeness pass.

#### Gap 7 — no zone-naming field exists

**(as built, PR8):** fixed. `config_flow.py`'s `async_step_schedule` now
collects a required `zone_name` text field (`config_flow.py:614`), defaulted
via the new `_suggest_zone_name(hass, climate_entity)` helper
(`config_flow.py:161-179`) which derives a suggestion from the selected
climate entity's HA `friendly_name`, stripping a trailing "Thermostat"/
"Climate" suffix (e.g. "Bedroom Thermostat" → "Bedroom") via
`_ZONE_NAME_SUFFIX_RE`, falling back to `""` (no suggestion) if the entity
has no resolvable friendly name — matching the "must NOT default to
'Climate Advisor'" requirement below exactly. The name is stored as
`entry.title` via `async_create_entry(title=zone_name or "Climate
Advisor", ...)` (`config_flow.py:602`) — the fallback only applies if the
user clears the (pre-filled, editable) field entirely, which still avoids a
silently-blank title. No uniqueness check against sibling zone names is
performed, matching the "cosmetic, not functional" design decision below.

The paragraphs immediately below describe the original design; the "as
built" note above confirms the shipped implementation matches it, including
the field-default requirement — no additional deviation to record for this
gap beyond the exact function/line locations.

`config_flow.py:559` (pre-PR8 line; see `config_flow.py:602` above for the
current location) hardcoded `title="Climate Advisor"` unconditionally in
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
`coordinator.py:1514` (`entry = self.hass.config_entries.async_get_entry(self._entry_id)`)
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

`async_unload_entry()` (`__init__.py:560`) calls `async_remove_panel(hass,
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

**(as built, PR5 — Step 5):** fixed. `async_remove_panel(hass,
PANEL_FRONTEND_PATH)` was moved inside the SAME `if not hass.data[DOMAIN]:`
block `async_unload_entry()` already used for `log_capture.uninstall()` and
(since PR4) the `ZONE_SCOPED_SERVICES` teardown loop — not a second,
parallel guard. It now fires only once the last zone unloads. The panel/view
registration guard flag (`_PANEL_HASS_DATA_KEY`, see the "(as built, PR5)"
note under [Gap 6](#gap-6--panelview-registration-needs-empirical-verification))
is also cleared inside this same block, so a zone added after every prior
zone was removed re-registers the shared panel/views instead of finding a
stale "already registered" flag left over from the torn-down instance.
Regression coverage: `tests/test_panel_zone_scoping.py` (new,
`build_headless_multi_zone()` + the `teardown_cleanup` assertion type) and
the pre-existing `tests/test_sim_harness_multi_zone.py::TestMultiZoneAssertionTypes::test_teardown_cleanup_unloads_and_reports_panel_state`,
which previously asserted `passed is False` to honestly document this bug
(per this project's no-fabricated-correctness testing doctrine) and now
asserts `passed is True` now that the bug is fixed.

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

**(as built, PR4 — Step 4):** fixed, in the same PR as Gap 5 — this is the
unload-side half of that fix, not a separate change. `async_unload_entry()`
gained a `for service_name in ZONE_SCOPED_SERVICES: hass.services.async_remove(DOMAIN, service_name)`
loop, reading the same five-element tuple Gap 5's registration guard reads,
so the registered-service set and the torn-down-service set cannot drift
apart from each other. This loop sits inside the existing
`if not hass.data[DOMAIN]:` block `async_unload_entry()` already used for
`log_capture.uninstall()` — i.e. it fires **only once the last zone unloads**,
not on every zone's individual unload. This is the correct lifetime for a
domain-wide (not per-zone) resource: since Gap 5's fix, the five services
are no longer bound to any single zone at all, so removing them while a
sibling zone is still live would strand that surviving zone with no way to
call any of the five services — a new, self-inflicted gap that would not
have existed before Gap 5's own fix. Concretely, this also means the
teardown-time defect described above — a deleted zone's now-defunct
`coordinator` still reachable through a lingering closure — can no longer
occur at all post-PR4, independent of the last-zone timing question: Gap 5's
fix already means no closure captures any specific zone's `coordinator`, so
there is nothing defunct left to silently act on even before the last zone
unloads.

Regression coverage: `tests/test_service_zone_scoping.py` (new) — unloads
one of two zones, asserts the surviving zone's services are still callable
via `hass.services.has_service()`, then unloads the last zone and asserts
all five are gone.

#### Gap 10 — residual zone-context gaps found by Issue #812's audit

**Not found by this document's original nine-gap review.** Discovered when a
user pushed back on a `zone_resolution_ambiguous` Repairs warning that didn't
match their mental model of "the dashboard already sends `entry_id`" — that
pushback prompted a full audit, which found four independent places where the
codebase still silently guessed which zone a request was for, despite this
document's Gap 4/6/8/9 fixes all being genuinely shipped and correct as far
as they went. All four share the same root shape as the original nine: code
that assumes a resolvable "the" zone instead of being handed one explicitly.
**Fixed, PR/commit `7ee7595` ("Fix #812: make Climate Advisor genuinely
zone-aware end-to-end"), 0.7.1 → 0.7.2.**

##### 10a — dashboard's own first-load requests hit the ambiguous fallback

**What:** the false claim corrected under [Transitional Safety
Window](#transitional-safety-window)'s "Scope of the signal" above.
`frontend/index.html`'s `_selectedEntryId` started `null` on every fresh page
load, seeded only after the first `/status` response came back and
`renderZoneSelector()` ran. Every one of `loadAll()`'s other 7 parallel
`load*()` calls fired zone-blind on every single page load of every
multi-zone install — not an edge case, the default first-touch behavior.

**Occupant-facing consequence:** a user with two zones who opens the
dashboard sees the ambiguous-fallback zone's data flash briefly (or, worse,
a mismatched combination as different cards resolve to different zones
depending on request timing) before the selector corrects it — every time
they load the page, not just once.

**Fix (`frontend/index.html`):** persist the selected zone to
`localStorage` (`ZONE_STORAGE_KEY = 'climate_advisor_selected_zone'`) and
seed `_selectedEntryId` from it synchronously at script init, before
`loadAll()` ever runs — so every session after the first is zone-correct
from the very first request. `renderZoneSelector()` and the zone-tab click
handler both call `_storeSelectedZone()` to keep the stored value current.
A genuine first-ever-visit (nothing stored yet, tracked via
`_hadStoredZoneAtInit`) still awaits `loadStatus()` once before firing the
other 7 calls, since no zone is knowable until the first response.

**Verification-pass regression (state plainly, per this project's
"Verification correction" precedent above under the WARNING throttle
fix):** the first version of this fix — `localStorage` persistence alone,
no self-heal — shipped a new bug that would not have existed before: a
stale stored zone (e.g. after deleting or reinstalling a zone, so the saved
`entry_id` no longer resolves) made the very first `loadStatus()` request
resolve to no coordinator (`api.py`'s `_get_coordinator()` returns `None`,
producing HTTP 503). The existing self-heal logic in
`renderZoneSelector()` only runs on a *successful* response, so it never got
a chance to run — the dashboard showed "Failed to load status" forever on
every reload, with the stale value never cleared. This was caught by this
session's own Verification pass before landing, not found later in
production. Fixed by detecting this specific case in `loadStatus()`'s own
`catch` block — a zone-scoped request that got back the 503 "not loaded"
error — and self-healing: clear the stale selection, retry once unscoped
(falling back to `get_default_coordinator()`, i.e. today's single-zone
behavior), and re-fire the other 7 calls that failed the same way. Guarded
to retry at most once so a genuine "Climate Advisor not loaded at all" 503
still surfaces as a real error instead of looping. **Lesson:** a self-heal
that only runs on the success path is not a self-heal for the failure mode
that matters most — the one that guarantees every subsequent request fails
the same way.

**Test coverage:** `tests/ui/zone-selector.spec.js` (Playwright, real
headless Chromium against the existing mock-server harness, actually
executed) — `"Issue #812: zone selection persists across a page reload"` and
`"Issue #812: a stored zone that no longer exists falls back gracefully"`.

##### 10b — Repairs flows hard-coded `entries[0]`, issue_ids were domain-wide

**What:** `WeatherEntityRepairFlow` and `ReloadNeededRepairFlow`
(`repairs.py`) both resolved their target config entry via
`hass.config_entries.async_entries(DOMAIN)[0]` — the exact "first entry"
singleton assumption Gap 4 fixed in `api.py`, still present, unfixed, in
`repairs.py`. Both issue types (`weather_entity_not_found`, `reload_needed`)
also used domain-wide, non-entry-scoped `issue_id` strings, so a second
zone's identical issue would collide with (or mask) the first's in HA's
Repairs list.

**Occupant-facing consequence:** in a 2+ zone install, clicking "Fix" on a
weather-entity Repairs card for zone B could silently patch zone A's config
entry instead — the same class of wrong-zone action Gap 5 (service handler
misdirection) was rated most-severe for, just reached through the Repairs UI
instead of a service call.

**Fix (`repairs.py`, `__init__.py`, `config_flow.py`):** issue_ids are now
entry-scoped (`f"weather_entity_not_found_{entry_id}"` /
`f"reload_needed_{entry_id}"`), with the `entry_id` threaded through
`ir.async_create_issue(..., data={"entry_id": entry_id})` into
`async_create_fix_flow()`, which passes it into the flow's constructor. Both
flows resolve their target entry via a shared `_resolve_target_entry(hass,
entry_id)` helper that prefers the captured `entry_id` and only falls back to
`async_entries(DOMAIN)[0]` when none was supplied (a pre-#812 unscoped issue
still open across an upgrade). A one-time migration in
`async_setup_entry()` clears any pre-existing unscoped issue on upgrade so it
doesn't linger as a dead, unfixable card.

**Test coverage:** `tests/test_repairs.py::TestMultiZoneRepairFlowTargeting`
(5 tests) — entry-scoped issue_ids don't collide across zones, fixing zone
A's weather issue touches only zone A, fixing zone B's weather/reload issues
touches only zone B, and single-zone behavior is unchanged (no entry_id
ambiguity possible with one zone).

##### 10c — zero zone attribution anywhere in logging

**What:** `log_capture.py`'s shared ring buffer (feeding the AI
Investigator's "System Errors/Warnings" section) had no zone field, and
every `_LOGGER` call site across the package is a bare
`logging.getLogger(__name__)` with no zone/entry tag. With 2+ zones running
concurrently, a WARNING from zone B's update cycle was indistinguishable
from zone A's in the shared buffer.

**Occupant-facing consequence:** investigating a problem in zone A's
dashboard could show zone B's warnings mislabeled as zone A's (or vice
versa) — actively misleading the occupant (or the AI Investigator acting on
their behalf) about which zone actually has a problem.

**Fix (`log_capture.py`, `coordinator.py`, `__init__.py`,
`ai_skills_context.py`):** a `ContextVar`-based zone tag, owned by
`log_capture.py` (`_current_zone_label`, set via `zone_scope(zone_label)`,
read via `current_zone_label()`), tags every captured record with the
active zone (or `None` if captured outside any `zone_scope()`).
**Important technical finding, confirmed empirically, not assumed:**
`ContextVar`s do **not** propagate into `hass.async_add_executor_job()` —
HA's wrapper submits to the `ThreadPoolExecutor` via `executor.submit()`
directly, without `contextvars.copy_context().run(...)`, so a zone label set
in the calling coroutine reads back as the ContextVar's default inside the
executor thread. A standalone asyncio + contextvars + `ThreadPoolExecutor`
script reproduced this directly. The fix uses explicit
`bind_zone_for_executor()` wrapping at every executor-job call site
(`coordinator.py`'s `_executor_job()`, and each zone-scoped service handler
in `__init__.py` that calls `learning.save_state()`/`learning.reset()` via
the executor) rather than relying on ambient propagation. `zone_scope()`
itself does cover directly-awaited async code and `hass.async_create_task()`
work, which inherit the current `contextvars.Context` automatically.
`ai_skills_context.py`'s `build_event_log_context()` now filters captured
records to `this_zone_label` (the investigated coordinator's own
`zone_label`) plus any untagged record, and renders an untagged record's
zone explicitly as `"unknown zone"` rather than guessing or silently
including/excluding it.

**Test coverage:** `tests/test_log_capture.py` — 203 new lines, including
`test_concurrent_coordinators_do_not_cross_contaminate_zone_tags` (genuinely
concurrent via `asyncio.gather`, not sequential awaits — the failure mode
this fix exists for is concurrency, so the test must actually be
concurrent), `test_contextvar_does_not_cross_executor_boundary_unwrapped`
(proves the executor-boundary finding above, not just documents it),
`test_bind_zone_for_executor_restores_zone_inside_executor_thread`, and
`test_ai_skills_context_filters_to_investigated_zone`.

##### 10d — `zone_registry` fallback order was non-deterministic

**What:** `get_default_coordinator()`'s two defensive fallback branches
(covering the "shouldn't happen in practice" cases where `hass.data[DOMAIN]`
disagrees with `hass.config_entries.async_entries()`) used
`next(iter(entries.values()))` — dict-insertion order, not guaranteed stable
across restarts, for a function whose entire job is picking a deterministic
"the" zone.

**Occupant-facing consequence:** in the rare case either defensive branch
actually fires, which zone a zone-unaware request resolves to could change
from one restart to the next with no configuration change — the WARNING
this branch already logs would name a different "picked" zone across
restarts, actively confusing anyone trying to diagnose why behavior seemed
to move between zones.

**Fix (`zone_registry.py`):** both branches now tie-break via
`sorted(entries.items(), key=lambda kv: kv[0])[0][1]` — a deterministic sort
on `entry_id` (stable, assigned once by HA and never changes), replacing
`next(iter(entries.values()))` in both places.

**Test coverage:**
`tests/test_zone_registry.py::test_defensive_empty_config_entries_picks_lowest_entry_id_regardless_of_dict_order`
and
`test_defensive_no_matching_loaded_entry_picks_lowest_entry_id_regardless_of_dict_order`
— both scramble `hass.data[DOMAIN]`'s dict-insertion order first, proving the
result is order-independent, the same distinction this document's own Gap 4
fallback test already proved once for the happy-path branch.

##### 10e — every zone independently sent its own daily briefing notification (Issue #817 Part 3/4)

**What:** `_async_send_briefing()` unconditionally sends push+email whenever
`push_briefing`/`email_briefing` are true, with zero awareness of sibling
zones. Correctly, from a zone-isolation lens, each zone computes its own
correct daily plan — but from a notification-count lens, N correctly-computed
zones means N pushes/emails to the same person for what reads as the same
kind of message ("your plan for the day"). #812's audit explicitly evaluated
briefing generation from the first lens ("NOT broken") — no one had yet
evaluated it from the second.

Separately, the dashboard's Regenerate button and the debug tab's Send
Briefing button both called the exact same code path, so a user refreshing
what's on their own screen (already looking at it) forced a real push/email
identical to the debug tab's deliberate manual test send — no distinction
between the two existed at the API layer.

**Occupant-facing consequence:** on any multi-zone install, the person
receiving briefing notifications got one copy per zone every morning, and
clicking Regenerate on the dashboard silently re-sent a real notification
they didn't ask for.

**Fix:**
- `const.py`: new `CONF_BRIEFING_NOTIFICATIONS_ENABLED`
  (`briefing_notifications_enabled`), category `"notifications"`, default
  `True`.
- `zone_registry.py`: new `default_briefing_notifications_enabled(hass,
  entry_id=None)` — the stably-first zone (same `async_entries(DOMAIN)`
  order as `get_default_coordinator()`/10d above) defaults to `True`; every
  other zone defaults to `False`. Shared by both the v19→v20 migration
  (`__init__.py`) and `config_flow.py`'s first-run default for a brand-new
  zone — one implementation of "pick the stably-first zone," not two that
  could silently diverge.
- `coordinator.py`: `_async_send_briefing()` gains `send_notifications` and
  `respect_notification_mute` parameters. Only the scheduled `briefing_time`
  trigger (via the new `_async_send_briefing_scheduled()` wrapper, registered
  with `async_track_time_change` in place of the bare method) passes
  `respect_notification_mute=True` — both dashboard buttons call
  `_async_send_briefing()` directly and never set it, so a manual send always
  means what it says regardless of a zone's mute state. Regenerate passes
  `send_notifications=False`; the debug tab's Send Briefing button leaves it
  at the default `True`.
- `api.py`: `ClimateAdvisorSendBriefingView.post()` reads an optional
  `{"notify": bool}` JSON body (default `True`, so a caller sending no body —
  the debug button — is unaffected).
- `frontend/index.html`: the Regenerate button now posts `{notify: false}`.

**Test coverage:** `tests/test_coordinator.py`
(`TestBriefingSameCycleReuse`, `TestBriefingNotificationGating`),
`tests/test_api_send_briefing.py`, `tests/test_config_flow.py`
(`TestMigrationV19ToV20`, the new notifications-step toggle tests in
`TestNotificationsStep`).

##### Known residual gap — `api.py` itself is not zone-scoped in logging

**Explicitly flagged, not fixed by #812, not hidden.** `api.py`'s own
executor-job/handler code was not wrapped in `log_capture.zone_scope()` —
confirmed by grep: `zone_scope`/`bind_zone_for_executor` appear in
`coordinator.py` and `__init__.py` but nowhere in `api.py`. A warning raised
while `api.py` is servicing one zone's dashboard request is tagged
`"unknown zone"` by `ai_skills_context.py` rather than mis-attributed to the
wrong zone — this is fail-safe by design (10c's fix explicitly renders
untagged records as `"unknown zone"` instead of guessing), not a defect —
but it is also not full coverage. A future pass wrapping `api.py`'s request
handlers in `zone_scope(entry_id)` (once the request's `entry_id` is
resolved) would close this and let the Investigator attribute API-layer
warnings to the right zone instead of showing them as unknown.

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
`coordinator.py:483` already gives every zone today. Config-entry-per-zone remains
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
independent thermal-model fitting for free — that IS `coordinator.py:483`'s
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
because this codebase's existing `CONFIG_METADATA` loop (`api.py:549-575`, was
`:535-561` pre-PR7 — PR7's `zone_registry` import and `_get_coordinator()`
growth shift everything below it by +14 lines,
`ClimateAdvisorConfigView.get()`) already treats strings and lists differently:

- **Plain-string fields** (e.g. `climate_entity`, a single entity_id, `category:
  "core"`, not flagged `sensitive` in `const.py`) pass through the loop unredacted.
  The only redaction check the loop applies is `if key == "notify_service" or
  meta.get("sensitive")` (`api.py:556`, was `:542` pre-PR7), which `climate_entity` doesn't match, so it
  falls through to `self.json(...)` as-is. This is the correct precedent for a
  zone's `climate_entity` and entry name: both are plain identifiers, same category
  as `notify_service` is NOT (an appliance/room identifier, not personal info), and
  both would be sent the same way `climate_entity` already is today.
- **List-typed fields** (e.g. `door_window_sensors`, an `EntitySelector(multiple=True)`
  list, `config_flow.py:414-419`/`928-936`) do **not** reach the frontend as raw
  entity_ids at all — confirmed `door_window_sensors` never appears anywhere in
  `api.py`. It only reaches the frontend through the same generic
  `CONFIG_METADATA` loop, which for any list-typed value does `if isinstance(value,
  list): value = f"{len(value)} configured"` (`api.py:562-563`, was `:548-549`
  pre-PR7) before the value is
  appended to the response. The entity_ids themselves are never transmitted — only
  a count is.

The actual transmission of `climate_entity` (and every other `CONFIG_METADATA`
field) happens in `ClimateAdvisorConfigView.get()`'s loop, `api.py:549-575`
(was `:535-561` pre-PR7; distinct from `api.py:114`'s `hass.states.get(...)`
call, was `:100` pre-PR7, which is a *use*
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

1. **PR1 — Diagnostics hook (`diagnostics.py`). DONE (Phase A).** Zero dependencies. Ships
   first so every step below can be debugged with a real downloadable bundle
   from day one. See [Diagnostics and Field Feedback](#diagnostics-and-field-feedback).
2. **PR2 — Test harness: drive real two-entry setup/unload. DONE (Phase A).** Zero
   production-code dependency. Required before PR4/PR5's fixes (both live
   inside `async_setup_entry()`/`async_unload_entry()`) can be
   regression-tested at all. See
   [Testing Without Multi-Zone Hardware](#testing-without-multi-zone-hardware).
   Its build starts concurrently with PR3, not after PR3 finishes — the
   harness is written to model both of PR3's possible outcomes as parallel
   test variants, so PR5's design work isn't blocked on PR3 completing first.
3. **PR3 — Empirical spike, NOT RUN (deliberate decision, 2026-09-01).**
   Originally: stand up two config entries against a real/dev HA instance and
   observe whether `async_setup_entry`'s panel registration raises on a
   duplicate `frontend_url_path`, and whether the first coordinator's update
   loop is already running when it does. The only HA access available in this
   worktree (`.deploy.env`) points at the project owner's live production
   instance — deliberately triggering a duplicate-registration crash path
   there was judged too risky to run unsupervised, and the owner explicitly
   chose **not** to run it live. **Decision: Phase B is designed against the
   worst-case outcome (b) — the first zone's coordinator/automation loop is
   already running by the time panel registration would raise — as a
   documented assumption, not a confirmed fact.** This means PR5's fix
   (reordering panel/service/view registration before `coordinator.async_setup()`)
   ships unconditionally rather than being gated on which outcome PR3 found.
   **Open validation item, not yet closed:** confirm this assumption against a
   real HA instance (dev or production, at the owner's discretion) before or
   shortly after this branch ships — if outcome (a) turns out to be true
   instead (clean crash before the control loop starts), PR5's reordering is
   still correct but unnecessary defense-in-depth, not a fix for a live gap.
4. **PR4 — Service-handler scoping and unregistration (Gaps 5 and 9). DONE
   (Step 4).** Safety-critical; no dependency on PR3's result. Shipped as a
   required `call.data["entry_id"]` field on all five `ZONE_SCOPED_SERVICES`,
   resolved at call time via `_resolve_zone_coordinator()` (raises
   `ServiceValidationError` for an unknown/unloaded entry_id) rather than a
   closure over a specific zone's `coordinator` — plus a `has_service()`
   registration guard so re-registering identical closures on every
   additional zone's setup is a no-op, and the matching
   `hass.services.async_remove()` teardown loop in `async_unload_entry()`,
   guarded to fire only once the last zone unloads. See
   `tests/test_service_zone_scoping.py`.
5. **PR5 — Panel/view registration scoping on setup and unload (Gaps 6 and
   8). DONE (Step 5).** Shipped WITHOUT running PR3's empirical spike — the
   actual fix is "register the shared REST views/static path/panel ONCE,
   domain-wide, guarded" (mirroring PR4's `has_service()` pattern via a new
   `_PANEL_HASS_DATA_KEY` flag), not the originally-sketched per-entry-unique
   `frontend_url_path`/reordering approach. This closes Gap 6's safety
   concern independent of PR3's outcome — see the "(as built, PR5)" note
   under [Gap 6](#gap-6--panelview-registration-needs-empirical-verification)
   for the full reasoning. `async_unload_entry()`'s Gap 8 fix folds
   `async_remove_panel()` into the SAME `if not hass.data[DOMAIN]:` guard
   already used for `log_capture.uninstall()`/PR4's service teardown, not a
   second guard. See `tests/test_panel_zone_scoping.py`.
6. **PR6 — Entry-scoped persistence (Gaps 1-3). DONE (Phase A).** No dependency on PR1-PR5.
   `LearningEngine`, `StatePersistence`, `ChartStateLog` all take
   `entry.entry_id` into their filename via `storage_paths.py`, each with a
   one-time migration mapping existing single-entry data to that entry's new
   scoped filename. See the "(as built, PR6)" note under
   [Gap 1](#gap-1--learningengine-db-collision) for the one deviation
   (falsy-`entry_id` fallback) from the design below.
7. **PR7 — `api.py` entry-scoping + zone registry + Transitional Safety
   Window (Gap 4). DONE (Phase C, Step 7).** Needed PR6's entry-scoped
   backing stores to select between. Ships the new `zone_registry.py` module
   (`get_coordinator`/`iter_coordinators`/`get_default_coordinator`) and
   replaces `_get_coordinator()`'s first-entry selection with entry_id-aware
   resolution (a query parameter, per `api.py:758`'s `hours` precedent)
   across all 21 call sites. Also ships the
   [Transitional Safety Window](#transitional-safety-window) fix. See the
   "(as built, PR7)" notes under [Gap 4](#gap-4--apipy-first-entry-selection-entire-rest-surface)
   and [Transitional Safety Window](#transitional-safety-window) for what
   actually shipped.
8. **PR8 — Config-flow zone naming (Gap 7). DONE (Phase A).** No hard dependency, but has no
   consumer until PR9. Add a name field stored as `entry.title` (per the hard
   requirement above — not a placeholder string with no accessor), so PR9
   and the future zone-influence selector both have something real to select
   on.
9. **PR9 — Dashboard zone selector. DONE (Phase D, Step 9 — final step).**
   Depended on PR4-PR8 all being zone-safe and named — the first PR that
   assumes the backend is actually zone-safe. Shipped in the same
   `feature/796-multi-zone-support` branch as PR7 (see [Release
   labeling](#release-labeling--what-was-actually-followed)), closing the
   Transitional Safety Window before either reaches `main`. Renders only
   when `zone_count > 1`, exactly as designed — see the "(as built, PR9)"
   note under [Gap 4](#gap-4--apipy-first-entry-selection-entire-rest-surface)
   for what actually shipped: `zone_registry.list_zones()`,
   `ClimateAdvisorStatusView`'s `zones`/`zone_count` fields, the
   `.zone-tabs`/`.zone-tab-btn` selector row, and the
   `_selectedEntryId`/`apiFetch()` parameterization covering 19 of 21 call
   sites. See "Conditional rendering" under
   [Resolved Questions](#dashboard-a-zone-selector-over-the-existing-card-layout-not-a-new-comparisonaggregation-card)
   for the design rationale.
10. **PR10 (independent track, no dependency on PR1-PR9) — the
    automation.py/coordinator.py shared indoor-temp-read fix. DONE (Phase A).**
    Independently valuable, no dependency on the zone work — fixes a live
    single-zone bug today, and the dedup work found a second, previously
    undocumented bug in the process (see
    [Carried-Over Citations](#carried-over-citations)). Can ship whenever
    convenient, before or after the rest of this list.

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
   `hass.data.setdefault(DOMAIN, {})` (`__init__.py:428`, was `:363`
   pre-Phase-B, `:423` pre-Phase-C) and
   `hass.data[DOMAIN][entry.entry_id] = coordinator` (`__init__.py:496`, was
   `:431` pre-Phase-B, `:491` pre-Phase-C) work
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

**(as built, PR2):** all three additions are done and match this design.
`build_headless_multi_zone()` (`tools/sim_harness/build_coordinator.py:251-388`)
takes `zone_count`, `configs`, `start_time`, `config_dir` and returns
`(zones, fake_hass, scheduler)` where `zones` is `{zone_label: {"coordinator":
..., "entry": ConfigEntry, "climate_entity": str}}` — one dict entry per
zone, in setup order, matching real `async_entries(DOMAIN)` ordering because
`fake_hass.config_entries.register_entry(entry)` is called before
`async_setup_entry()` for each zone, mirroring real HA's own registration
order. `config_dir` defaults to being **shared across zones on purpose** —
each zone's persistence filenames are expected to already be entry-scoped in
production (PR6), so a collision there is exactly what a
`cross_zone_isolation`/`teardown_cleanup` scenario is designed to catch.
`tests/test_sim_harness_multi_zone.py` (252 lines) exercises the harness
extension itself; `tests/test_storage_paths.py` (244 lines) covers
`storage_paths.py` directly.

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

**(as built, PR2):** the three assertion evaluators and the schema validator
live in the new `tools/sim_harness/multi_zone_assertions.py` (355 lines),
matching the shapes above exactly. Function signatures future scenario
authors (Steps 4/5/7) should call directly:

- `validate_zones_schema(scenario: dict) -> None` — no-ops if `"zones"` is
  absent; raises `ValueError` with a specific message for a malformed
  `"zones"` array or a malformed assertion of one of the three new types
  (never silently accepts a broken shape).
- `check_cross_zone_isolation(zones, fake_hass, assertion) -> (bool, str)` —
  async. Reads `before = resolve_dotted_field(coordinator, unaffected_field)`,
  calls the named service unscoped (matching how a real user would call it —
  production's service handlers take no zone-targeting parameter today), then
  reads `after` and asserts `before == after`. **Caveat for scenario
  authors:** the asserted field must resolve to a value that actually changes
  when the service runs on a freshly-built coordinator — an all-defaults
  `LearningState` needs seeding first, or the assertion passes vacuously.
- `check_service_registry_binding(zones, fake_hass, assertion) -> (bool, str)`
  — sync. Walks the registered handler's closure free-variables
  (`__code__.co_freevars` / `__closure__`) for `coordinator` or `entry` and
  identity-matches the captured object against the known zones. **Built
  against pre-PR4 production code and now superseded, not just untested:**
  as of PR4 (Gap 5, Step 4), the five handlers no longer close over any
  per-zone `coordinator` or `entry` at all — each resolves its target zone
  fresh, at call time, via `_resolve_zone_coordinator(hass, call)` reading
  `call.data["entry_id"]`, so the closures' free variables are just `hass`
  and helper functions. This check would find nothing to match against
  current handlers; `tests/test_service_zone_scoping.py` confirms this by
  using `cross_zone_isolation` (calling the real service end-to-end and
  observing the outcome) instead of `service_registry_binding` for its Gap
  5 coverage. `service_registry_binding` remains defined in
  `multi_zone_assertions.py` and is not deleted here — it is unused, kept as
  a record of the pre-PR4 bug shape, not exercised as regression coverage.
- `check_teardown_cleanup(zones, fake_hass, assertion) -> (bool, str)` —
  async. Performs the unload itself (via the REAL `async_unload_entry()`) —
  a `teardown_cleanup` assertion IS the unload event, not a check that runs
  after some other event already unloaded it.
- `check_multi_zone_assertion(zones, fake_hass, assertion) -> (bool, str)` —
  the dispatcher by `assertion["type"]`; returns `(False, reason)` for a
  type it doesn't own, so a caller can fall back to `outcomes.py`'s existing
  single-result `check_assertion()` for ordinary assertions in the same
  scenario file.

**Deliberately NOT wired into `tools/simulate.py`'s single-result execution
model** (`run_scenario_production()` / `ClimateSimulator._check_assertion()`)
— that model is built around one `ProductionRunResult` for one engine/
coordinator run. Driving a multi-zone scenario through `simulate.py` end to
end (new event types like `unload_entry`, a "which zone does this event
target" dispatch layer, MANIFEST/report changes) is real, separate work
explicitly deferred to whichever step first authors a zones-scenario. Both
entry points above take the exact `(zones, fake_hass)` shape
`build_headless_multi_zone()` returns, not a raw scenario file, so calling
them today will not need to change when the `simulate.py` wiring is added
later.

**Formerly an OPEN QUESTION, now moot (as built, PR4):** whether `hass.services`
exposes closure/coordinator identity introspectably enough at runtime to
implement `service_registry_binding` and the `active_service_bindings`
diagnostics field (see [Diagnostics and Field Feedback](#diagnostics-and-field-feedback)
below) without relying on undocumented HA internals. PR4 (Gap 5, Step 4)
sidestepped this question rather than answering it: services no longer close
over a per-zone `coordinator`/`entry` at all, so there is no closure identity
left to introspect, through public APIs or otherwise. No PR3-adjacent
empirical verification was ever needed for this specific question.

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

### Known test-infra gap (as built, PR7): `homeassistant.util.dt` parent-attribute shadowing

Confirmed by reading `tools/sim_harness/ha_stubs.py`: the same failure class
Issue #519 already fixed for `entity_registry`/`device_registry` has a
sibling that is still **open**. `_MockHomeAssistantModule`'s auto-mock
behavior means `from homeassistant.util import dt as dt_util` inside a
function-local import resolves via the **parent** `homeassistant.util`
mock's attribute access, not `sys.modules["homeassistant.util.dt"]`
directly — an auto-mocked attribute access on a `MagicMock` parent returns a
brand-new, unrelated `MagicMock`, not the real registered submodule. Issue
#519's fix (`ha_stubs.py:414-423`) pins `homeassistant.helpers`'s
`entity_registry`/`device_registry`/`entity` attributes to their real
registered submodules for exactly this reason. `homeassistant.util` and
`homeassistant.util.dt` are both registered mock modules
(`_HA_MODULES`, `ha_stubs.py:50-51`), but **no equivalent
`sys.modules["homeassistant.util"].dt = sys.modules["homeassistant.util.dt"]`
pin exists** — confirmed via grep, zero hits for `.dt =` or `util.dt` in the
setup block. Any test exercising a code path that does a function-local
`from homeassistant.util import dt as dt_util` (four call sites in `api.py`
alone: lines 153, 448, 763, 946 — the last being
`ClimateAdvisorEventLogView.get()`) will see `dt_util.now()` etc. resolve to
an auto-mocked `MagicMock`, not a real `datetime`, unless something else in
the test's setup happens to route around it.

**What Phase C actually did about it, not what it should have done:**
`tests/test_repairs.py`'s `test_cross_zone_isolation_unaffected_by_ambiguous_issue_lifecycle`
hit this directly — `force_reclassify` drives a real coordinator
data-refresh cycle that calls `dt_util.now()` internally, which only
resolves to a real datetime while `build_headless_multi_zone()`'s
`FakeScheduler` is installed (that scheduler patches the relevant `dt_util`
accessors for its own duration, but `build_headless_multi_zone()` only keeps
it active for its own zone-setup phase). The test's fix was to re-enter
`scheduler.installed()` for the duration of the service call
(`tests/test_repairs.py`, see the in-code comment on that test) — a
test-local workaround, not a `ha_stubs.py` fix. Separately,
`tests/test_api_multi_zone.py`'s 4-of-21 representative sample (see its
module docstring) does not include `ClimateAdvisorEventLogView` — the one
`api.py` view whose `get()` does the same function-local `dt_util` import —
though no in-code comment in that file states this exclusion was deliberate,
so treat that specific connection as circumstantial, not confirmed.

**Status:** open. Fixing this properly means extending Issue #519's exact
pattern — `sys.modules["homeassistant.util"].dt = sys.modules["homeassistant.util.dt"]`
alongside the existing `helpers.entity_registry`/`device_registry`/`entity`
pins in `ha_stubs.py`'s setup block — but that is a harness change with
broader blast radius (every existing test that already tolerates
`dt_util.now()` as a `MagicMock` in some code path) than Phase C's Step 7
scope covers. Left as known debt for whoever next needs a real `dt_util`
value inside a function-local import in the harness, rather than fixed
speculatively here.

## Diagnostics and Field Feedback

### The gap in the existing mechanism

`dump_diagnostics` (`__init__.py:479-497`, confirmed exact fields: `version`,
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
  Gap 7 placeholder (`"Climate Advisor"` — pre-PR8 this was hardcoded
  unconditionally; post-PR8, `config_flow.py:602` only falls back to it if the
  user clears the zone-name field entirely, see the "(as built, PR8)" note
  under [Gap 7](#gap-7--no-zone-naming-field-exists)) or a real user-set name;
  a placeholder title in a report is itself signal that this install predates
  PR8's fix, or that the user cleared the suggested name
- `entry_setup_order` — this entry's position in
  `hass.config_entries.async_entries(DOMAIN)`'s stable order, the same
  accessor the Transitional Safety Window fallback uses in production —
  directly diagnostic for Gap 5/9-class reports
- `active_service_bindings` — originally scoped as "which entry_id each of
  the five domain-scoped services is currently bound to," the single most
  direct diagnostic for "I called reset_learning_data on zone B and zone A's
  data changed." **(as built, PR4):** this question is now moot rather than
  answered — see the "(as built, PR4)" note in
  [Testing Without Multi-Zone Hardware](#testing-without-multi-zone-hardware).
  Since PR4, services resolve their target zone per-call from a required
  `entry_id` field (validated fail-closed via `ServiceValidationError`), so
  there is no static binding left to report; the field instead states that
  design explicitly
- The existing `dump_diagnostics` fields (`debug_state`, chart-data counts,
  `learning_summary`, `config` minus `notify_service`, `briefing_state`)

**Redaction:** reuse the existing `CONFIG_METADATA`/`api.py:556` (was `:542`
pre-PR7) convention
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

**(as built, PR1):** all of the above shipped as designed, with one
deliberate security improvement over the pre-existing `dump_diagnostics`
handler worth documenting explicitly. The old handler's `"config"` field was
built as `{k: v for k, v in coordinator.config.items() if k != "notify_service"}`
— it *omitted* the `notify_service` key entirely, but never touched
`ai_api_key` (the codebase's one `"sensitive": True`-flagged `CONFIG_METADATA`
key, `const.py:833-840`) at all, so a raw API key would have flowed straight
into the logged diagnostic dump. The new shared
`async_get_diagnostics_payload()` (`diagnostics.py:35-91`, was `:35-86`
pre-Phase-B — the PR4 note added to the `active_service_bindings` field's
comment block, see [Gap 5](#gap-5--service-handler-misdirection-most-severe),
grew the function by 5 lines) instead builds
`"config": dict(coordinator.config)` (the full, unfiltered config) and relies
entirely on the final `async_redact_data(payload, TO_REDACT)` call
(`diagnostics.py:91`, was `:86` pre-Phase-B) to protect both `notify_service`
and `ai_api_key`
(`TO_REDACT = {"notify_service"} | {key for key, meta in
CONFIG_METADATA.items() if meta.get("sensitive")}`, `diagnostics.py:32`,
unchanged) —
and HA's `async_redact_data` **replaces** a matching key's value with the
literal string `"**REDACTED**"` wherever it appears in the payload (including
nested dicts), rather than dropping the key, which is a stronger guarantee
than the old handler's plain omission (a value that's present-but-redacted
can't be mistaken for "this config has no notify service configured" the way
a silently-missing key could). `handle_dump_diagnostics` (`__init__.py:554-570`,
was `:479-497` pre-Phase-B — now nested inside PR4's `has_service()` guard
block, see [Gap 5](#gap-5--service-handler-misdirection-most-severe))
was changed to call this same shared helper, so the log-only service call
gets the same fix for free — this closes a real, previously-undocumented gap
in the log-only path, not just a refactor.

**(as built, PR1 original / PR4 superseded):** at PR1 time, `active_service_bindings`
shipped as an explicit, honest limitation rather than fabricated data —
`diagnostics.py` returned the literal string `"not introspectable via public
HA APIs — see docs/multi-zone-spec.md 'Diagnostics and Field Feedback' open
question"` for every payload, because HA's public `hass.services` API
surfaces only registered service names/schemas, not a bound closure's
captured `coordinator` variable.

**PR4 (Step 4) changed the underlying question, so this field's text changed
with it** — not because the introspection question got answered, but because
it stopped applying. Once services resolve their target zone per-call from a
required `entry_id` field (`_resolve_zone_coordinator()`, `__init__.py`)
instead of closing over one zone's `coordinator` at registration time, there
is no static "current binding" left for any diagnostics field to report,
introspectable or not. `diagnostics.py` (`active_service_bindings`, near the
top of `async_get_diagnostics_payload()`) now returns a literal string
describing that design directly: services are registered once, domain-wide,
and resolve their target zone per-call from `entry_id`. `tests/test_diagnostics.py`'s
`test_active_service_bindings_reports_call_time_resolution_not_a_static_binding`
covers this.

The harness-only `check_service_registry_binding()` (`multi_zone_assertions.py`)
was built at PR2 time to answer a *different* question than this field ever
tried to — "can the test harness verify binding via closure introspection"
(yes, because a `FakeHass` is a plain-Python object with inspectable
closures) vs. "can production code introspect it through public HA APIs" (a
question PR4 made moot for production, not merely unresolved). Because PR4
removed the `coordinator`/`entry` closure variables this harness check walks
for, it is no longer exercisable against current production handlers — see
the "(as built, PR4)" note under
[Testing Without Multi-Zone Hardware](#testing-without-multi-zone-hardware)
for how `tests/test_service_zone_scoping.py` covers Gap 5 instead
(`cross_zone_isolation`, calling the real service end-to-end). PR3's
empirical spike was never about this question at all — it concerns Gap 6's
panel/view registration timing, not service binding — and remains
deliberately un-run for the separate reasons documented under Gap 6.

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

### Release labeling — what was actually followed

This section originally proposed folding each PR (PR1-PR10) into the existing
flat per-PR version-bump/fix_history/CHANGELOG/merge flow, with no dedicated
branch and no new channel. That is not what happened in practice, once
implementation started: a single-branch approach was used instead, for a
reason the flat-PR plan didn't anticipate — ten PRs landing independently
against `main`, each bumping `VERSION` and merging separately, would have put
a partially-implemented multi-zone surface (e.g. Gap 5's `entry_id`-required
services shipped, but PR9's dashboard selector not yet) in front of every
single-zone install for the days/weeks between PR1 and PR9 landing. The
actual plan, and the one this branch has followed for its entire life:

- One dedicated branch, `feature/796-multi-zone-support`, holds every step
  (PR1 through PR9/Step 9 and beyond) rather than merging steps into `main`
  one at a time.
- `VERSION`/`manifest.json` are pinned at `0.7.0` for the branch's entire
  life — not bumped per-step the way a normal flat PR would — since no
  step is independently releasable; `0.7.0` is claimed once, when the whole
  feature merges.
- `fix_history.jsonl` entries (one per closed Gap/PR, via
  `tools/add_fix_entry.py --issue 796 --version 0.7.0 ...`) and the matching
  `CHANGELOG.md` `## [0.7.0]` section are added at merge time, covering every
  gap fixed across the branch's life in one batch — **not yet done as of this
  writing** (confirmed: zero `"796"` matches in `fix_history.jsonl`, no
  `0.7.0` section in `CHANGELOG.md` yet); this is deferred to branch landing
  by design, not an oversight, since a partial batch mid-branch would misstate
  what actually shipped.
- The branch merges to `main` as a reviewed PR (or a small number of
  logically-grouped PRs, if the final diff is large enough that a single
  review is impractical) only once every step passes — golden simulations,
  the full pytest suite, and (new as of Step 9) the Playwright UI suite.
- The CHANGELOG/GitHub Release callout this section originally specified is
  still the right label and is unchanged in substance: an explicit "Multi-Zone
  Support (Experimental)" entry, using the existing
  `fix_history.jsonl --user-summary`/CHANGELOG mechanism, encouraging early
  adopters to watch subsequent patches and use the symptom checklist above
  when filing issues.
- A `v0.7.0` GitHub Release is cut once the branch merges: `gh release create
  --draft v0.7.0 ...`, human review of the draft, then
  `gh release edit v0.7.0 --draft=false` — same draft-then-publish mechanism
  this project's Release Process already uses for every versioned release, no
  new infrastructure.

This is still a labeling convention, not a new release channel or
infrastructure — HACS/Releases-API mechanics are untouched — but it is a
single-branch-then-merge shape, not the step-by-step flat-PR shape originally
proposed here.

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
  "active_service_bindings": "not applicable — since PR4, zone-scoped services ... resolve their target zone per-call from entry_id ...",
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
   entry, constructed exactly as they are today (`coordinator.py:483`,
   `coordinator.py:495-505`) — no shared mutable state is introduced between
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

- `AutomationEngine._get_indoor_temp_f()` (originally `automation.py:9691-9713`)
  was a second, independent "read indoor temp" implementation parallel to
  `coordinator._get_indoor_temp()` (originally `coordinator.py:3002-3050`), missing the
  plausibility guard (`_MIN/_MAX_PLAUSIBLE_INDOOR_F`, originally `coordinator.py:3014,3039`)
  that the coordinator's version had. 13 call sites in `automation.py`: 3535,
  3564, 3862, 3943, 3952, 4115, 6047, 7220, 7809, 7854, 8074, 8116, 9077 (unaffected
  by the fix below — these still call `self._get_indoor_temp_f()`, now a thin wrapper).
  Confirmed live, present-tense, exploitable bug — a bad sensor reading (e.g.
  999°F) flows unguarded into real HVAC decisions via the 5-min backstop timer
  `_thermo_backstop_task()`/`async_call_later` at `automation.py:9075`, and the
  door/window listener `handle_door_window_open()` at `automation.py:3511`.

  **(as built, PR10):** fixed, and via the fix the dedup work turned up a
  **second, previously-undocumented bug** in the same duplication: the
  original `automation.py` version's `climate_fallback` path had **no
  exception handling at all** around `float(temp)` — a non-numeric
  `current_temperature` attribute would raise uncaught, where the
  coordinator's equivalent path already caught `(ValueError, TypeError)`.
  This was a real gap, not just a refactor target — it is now fixed for both
  callers by construction, since both delegate to the same guarded helper.

  Shared logic is now `indoor_temp.resolve_indoor_temp_f()`
  (`indoor_temp.py:44-97`, new module, `indoor_temp.py:1-24`'s module
  docstring documents both bugs and the five-whys for why a plain module was
  used) — matching the `fan_status.py::resolve_untracked_fan_status()`
  precedent exactly as planned, not a mixin, since `ClimateAdvisorCoordinator`
  and `AutomationEngine` remain composed, not inheritance-related, elsewhere
  in the codebase. `AutomationEngine._get_indoor_temp_f()`
  (`automation.py:9876-9894`) and `ClimateAdvisorCoordinator._get_indoor_temp()`
  (`coordinator.py:3047-3061`) are now both thin callers passing their own
  fresh `self.hass`/`self.config` reads into the shared function — the
  cadence difference (automation.py's timers/listeners need fresh reads
  between coordinator cycles) is preserved exactly as designed: neither
  caller was changed to route through a coordinator-passed parameter, and
  the helper itself does no caching.
- `_build_predicted_indoor_future()` is DEFINED at `coordinator.py:9179-9188` (not
  a call site); real call sites are `coordinator.py:2384-2395`, `3423` (both in
  already-async contexts), and `8224` (inside `get_chart_data()`,
  `coordinator.py:8099`, whose only caller `api.py:357-359` (was `:343-345`
  pre-PR7) already wraps it in
  `await hass.async_add_executor_job(...)` — confirmed thread-safe, no gap).
  Cited here only for context; no zone-work dependency.
- `door_window_sensors` (`config_flow.py:414-419` setup, `928-936` options) —
  existing multi-entity `EntitySelector(multiple=True)` list precedent, cited
  only for contrast where relevant — not reused as the zone model (a zone is a
  config entry, not a list entry).
- `api.py:556` (was `:542` pre-PR7) — existing redaction pattern (`if key == "notify_service" or
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

- [`async_setup_entry`](../custom_components/climate_advisor/__init__.py#L361) — per-entry coordinator construction (already correct); setup ordering relevant to Gap 6
- [`handle_reset_learning_data`](../custom_components/climate_advisor/__init__.py#L504) — Gap 5's confirmed closure-capture bug
- [service registrations](../custom_components/climate_advisor/__init__.py#L460) — `respond_to_suggestion`, `force_reclassify`, `resend_briefing`, `dump_diagnostics`
- [`reset_learning_data` registration](../custom_components/climate_advisor/__init__.py#L510) — domain-scoped, overwritten by second entry's setup
- [REST view registration](../custom_components/climate_advisor/__init__.py#L517) — Gap 6
- [panel registration](../custom_components/climate_advisor/__init__.py#L521) — `async_register_built_in_panel`, fixed `frontend_url_path`, Gap 6
- [`_get_coordinator`](../custom_components/climate_advisor/api.py#L73) — Gap 4, **fixed, PR7 DONE** (was `#L72`, first-entry selection, 21 call sites, pre-PR7; now `entry_id`-aware, resolves via `zone_registry`)
- [`api.py` sensitive-field redaction](../custom_components/climate_advisor/api.py#L556) (was `#L542` pre-PR7) — existing precedent reused for the redaction question
- [`LearningEngine.__init__`](../custom_components/climate_advisor/learning.py#L679) — Gap 1, fixed-filename storage path (as designed); **now entry-scoped, PR6 DONE** — see `resolve_entry_scoped_path` below
- [`LEARNING_DB_FILE`](../custom_components/climate_advisor/const.py#L292) — Gap 1's fixed filename constant
- [`STATE_FILE`](../custom_components/climate_advisor/const.py#L280) — Gap 2's fixed filename constant
- [`StatePersistence`](../custom_components/climate_advisor/state.py#L25) (`state.py`) — Gap 2, **now entry-scoped, PR6 DONE**
- [`ChartStateLog`](../custom_components/climate_advisor/chart_log.py#L43) / `_CHART_LOG_FILE` (`chart_log.py`) — Gap 3, **now entry-scoped, PR6 DONE**
- [`async_create_entry` zone-name field](../custom_components/climate_advisor/config_flow.py#L602) — Gap 7, **fixed, PR8 DONE** (field itself at `config_flow.py#L614`, suggestion helper at `config_flow.py#L161`); pre-PR8 this cited the hardcode at `config_flow.py:559`
- [`AutomationEngine._get_indoor_temp_f`](../custom_components/climate_advisor/automation.py#L9876) — PR10 **DONE**; now a thin wrapper around `indoor_temp.resolve_indoor_temp_f()` (was a second, independent implementation at the old `automation.py:9691`, before the dedup)
- [`coordinator._get_indoor_temp`](../custom_components/climate_advisor/coordinator.py#L3047) — PR10 **DONE**; now also a thin wrapper around the same shared helper (was the correct/guarded version at the old `coordinator.py:3002`)
- [`indoor_temp.resolve_indoor_temp_f`](../custom_components/climate_advisor/indoor_temp.py#L44) (new, PR10 **DONE**) — the shared helper both classes now delegate to; also fixes a second bug found during the dedup (missing exception handling on automation.py's original climate_fallback path, see [Carried-Over Citations](#carried-over-citations))
- [`fan_status.py::resolve_untracked_fan_status`](../custom_components/climate_advisor/fan_status.py) — precedent pattern for PR10's extraction and for the module-vs-mixin decision (followed exactly, as built)
- [`coordinator.py:483`](../custom_components/climate_advisor/coordinator.py#L483) — existing per-entry `LearningEngine` construction that Gap 1's fix unlocks for free
- [`coordinator.py:495-505`](../custom_components/climate_advisor/coordinator.py#L495) — existing per-entry `AutomationEngine` construction, already correct
- [`zone_registry.py`](../custom_components/climate_advisor/zone_registry.py) (new, PR7 **DONE**; WARNING throttle added post-PR7, Verification fix) — Gap 4's fix; [`get_coordinator()`](../custom_components/climate_advisor/zone_registry.py#L95), [`iter_coordinators()`](../custom_components/climate_advisor/zone_registry.py#L100), [`get_default_coordinator()`](../custom_components/climate_advisor/zone_registry.py#L105) (line numbers shifted from `#L39,#L44,#L49` by the throttle fix's additions) — also the future Zone Influence feature's accessor surface; also now the accessor `_resolve_zone_coordinator()` (`__init__.py:392-423`, Gap 5's fix) delegates to instead of its own duplicate one-liner. [`_warn_once()`](../custom_components/climate_advisor/zone_registry.py#L65) and [`reset_warning_state()`](../custom_components/climate_advisor/zone_registry.py#L82) (both new, Verification fix) implement the once-per-distinct-outcome WARNING throttle — see "WARNING throttle fix" under Gap 4 above.
- `tests/test_zone_registry.py` (new, PR7 **DONE**; throttle test classes added post-PR7, Verification fix) — `get_coordinator`/`iter_coordinators`/`get_default_coordinator` across 0/1/2+ zones, including the single-zone fast path's `async_entries()` non-call, the multi-zone fallback's stable-order/WARNING-log behavior, the two previously-silent defensive branches now warning, and `TestGetDefaultCoordinatorWarningThrottle`'s throttle/reset coverage
- `tests/test_api_multi_zone.py` (new, PR7 **DONE**) — `_get_coordinator(hass, request)`'s `entry_id`-resolution and absent-`entry_id` backward-compat path, driven through 4 of the 21 `api.py` view classes
- `tests/test_repairs.py::TestZoneResolutionAmbiguousIssue` (new, PR7 **DONE**) — the `zone_resolution_ambiguous` Repairs issue's raise/clear lifecycle, driven through the real `async_setup_entry()`/`async_unload_entry()` via `build_headless_multi_zone()`
- [`storage_paths.py`](../custom_components/climate_advisor/storage_paths.py) (new, PR6 **DONE**) — Gaps 1-3's shared fix; [`resolve_entry_scoped_path()`](../custom_components/climate_advisor/storage_paths.py#L29) (single source of truth for entry-scoped filenames — falls back to the unscoped legacy path when `entry_id` is falsy, a deviation from the original design snippet, see the "(as built, PR6)" note under [Gap 1](#gap-1--learningengine-db-collision)) and [`migrate_legacy_storage_file()`](../custom_components/climate_advisor/storage_paths.py#L59) (one-time idempotent migration, not in the original design snippet but called for by this document's own Pre-conditions item 3)
- [`api.py:758`](../custom_components/climate_advisor/api.py#L758) (was `#L744` pre-PR7) — `request.query.get("hours", 12)`, the existing query-param precedent Gap 4's `entry_id` parameter followed, **PR7 DONE**
- [`coordinator.py:1514`](../custom_components/climate_advisor/coordinator.py#L1514) — `hass.config_entries.async_get_entry(self._entry_id)`, the `entry.title` read precedent Gap 7's fix must use
- [`coordinator.py:448`](../custom_components/climate_advisor/coordinator.py#L448) — `self._entry_id` attribute, read by the `entry.title` accessor above
- [`repairs.py:38,77`](../custom_components/climate_advisor/repairs.py#L38) — `hass.config_entries.async_entries(DOMAIN)` precedent for the Transitional Safety Window's deterministic fallback order
- [`const.py:1058-1059`](../custom_components/climate_advisor/const.py#L1058) — `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` (1058, 300s) / `THERMAL_FAN_SAMPLE_INTERVAL_S` (1059, 120s), cited in Zone Influence's data-flow-cadence analysis
- [`door_window_fsm.py:310-311`](../custom_components/climate_advisor/door_window_fsm.py#L310) — `DoorWindowFsmInputs.natural_vent_active`/`.whf_owns_hvac`, the communicating-automata precedent for the future Zone Influence FSM's cross-instance `Inputs` shape
- `build_coordinator.py:181` (`tools/sim_harness/build_coordinator.py`) — `build_headless_coordinator()`, constructs `ClimateAdvisorCoordinator` directly, bypassing `async_setup_entry()` entirely; the reason Gaps 5/6/8/9 have no regression coverage today. Left untouched by PR2 — single-zone tests keep this fast path.
- `build_coordinator.py:177-180` — comment confirming `__init__` never touches `async_track_*`/`hass.bus`, i.e. no setup-entry side effects occur via the direct-construction path
- [`build_headless_multi_zone()`](../tools/sim_harness/build_coordinator.py#L251) (`tools/sim_harness/build_coordinator.py:251-388`, new, PR2 **DONE**) — drives the REAL `async_setup_entry()`/`async_unload_entry()` per zone against one shared `FakeHass`; see the "(as built, PR2)" note under [Harness extension needed](#harness-extension-needed)
- [`multi_zone_assertions.py`](../tools/sim_harness/multi_zone_assertions.py) (new, PR2 **DONE**) — `cross_zone_isolation`/`service_registry_binding`/`teardown_cleanup` assertion evaluators plus `validate_zones_schema()`; see the "(as built, PR2)" note under [Golden scenario schema extension](#golden-scenario-schema-extension). Note: `service_registry_binding` is unused as of PR4 — see the "(as built, PR4)" note under [Testing Without Multi-Zone Hardware](#testing-without-multi-zone-hardware)
- `ha_stubs.py:179-229,317-319` (`tools/sim_harness/ha_stubs.py`) — existing `_MockConfigFlow`/`_MockOptionsFlow` realification precedent PR2's new config-entry stub follows (PR2 **DONE**)
- `fake_hass.py` (`tools/sim_harness/fake_hass.py`) — `.data`/`.config_entries` now added, PR2 **DONE**
- `tests/test_api.py:43-60` — existing hand-built `hass.data = {DOMAIN: {"entry_1": coord}}` `MagicMock()` precedent for the shape `build_headless_multi_zone()` now produces via the real setup path instead
- [`dump_diagnostics` fields](../custom_components/climate_advisor/__init__.py#L554) (`__init__.py:554-570`, was `:479-497` pre-Phase-B, PR1 **DONE** — now redirected through `diagnostics.py`'s shared helper, see the "(as built, PR1)" note under [Fix: implement HA's native diagnostics hook](#fix-implement-has-native-diagnostics-hook)) — original log-only diagnostics payload, superseded (not replaced) by the new `diagnostics.py` hook
- [`diagnostics.py`](../custom_components/climate_advisor/diagnostics.py) (new, PR1 **DONE**) — `async_get_config_entry_diagnostics()` / `async_get_diagnostics_payload()`, HA's native Download Diagnostics hook; see [Diagnostics and Field Feedback](#diagnostics-and-field-feedback)
- `docs/HA-BOUNDARY-EXCEPTIONS.md` — one active exception (learning-DB file); no new entry needed for `diagnostics.py`
- [HA Developer Docs — Implements diagnostics](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics/) — official signature/pattern for `async_get_config_entry_diagnostics`, `TO_REDACT`, and `async_redact_data()`
- [HA Developer Docs — Config Flow](https://developers.home-assistant.io/docs/core/integration/config_flow/) — config-entry title/naming guidance. `title_placeholders` governs the flow's own display title during setup/discovery, not a config entry's persisted `title`; Gap 7's actual fix (PR8, **DONE**) is the existing `async_create_entry(title=...)` call site (now `config_flow.py:602`, `title=zone_name or "Climate Advisor"`) taking a real value instead of the pre-PR8 hardcoded string — a mechanism this codebase already used. The page does not cover cross-entry selection or repeatable-item options-flow patterns, so the Zone Influence config-surface sketch and PR8's naming flow both remain this document's own design, not a documented HA pattern.
- Home Assistant developer-docs and community search results on `notify.persistent_notification` — confirms it is a fixed built-in notify target, not a user-configured notification service; the basis for keeping the Transitional Safety Window's mechanism on `issue_registry`/`repairs.py` rather than a notification call.
