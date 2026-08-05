<!-- Nav: ← [grace-periods-spec.md](grace-periods-spec.md) | → [automation.py](../custom_components/climate_advisor/automation.py) + [coordinator.py](../custom_components/climate_advisor/coordinator.py) + [fan_status.py](../custom_components/climate_advisor/fan_status.py) | ↔ [08-COMPUTATION-REFERENCE.md](08-COMPUTATION-REFERENCE.md) -->

# QuietCool RF Remote Timer & Speed Events — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does the occupant experience when they press a timer on the physical remote? | The whole-house fan runs for exactly the selected duration (1/2/4/8/12 hours) without Climate Advisor's own automation shutting it off partway through, the way it would with an un-communicated manual override. | [§ Occupant Impact](#occupant-impact) |
| What entity/attribute does the firmware expose, and what values does it emit? | An HA `event.*` entity (e.g. `event.quietcool_remote`). Each firmware-decoded remote press fires a state change with the command in `attributes["event_type"]`. Timer tokens: `timer_1h`, `timer_2h`, `timer_4h`, `timer_8h`, `timer_12h`, `timer_none`; speed tokens: `low`, `medium`, `high`. As of Issue #519, a second entity — `sensor.*` (e.g. `sensor.<device>_quietcool_speed`) — reports the current speed as continuously-readable ambient STATE, distinct from the discrete-press event entity. | [§ Firmware Event Contract](#firmware-event-contract) |
| How does a timer selection map onto CA's existing grace mechanics? | It does NOT create a new predicate. A timer press calls the SAME `handle_fan_manual_override()` the physical-fan-on detection path already uses, with an optional `duration_override` (seconds) that bypasses the configured `manual_grace_seconds` for that one override. | [§ Design — One Entry Point](#design--one-entry-point) |
| What happens when the user presses a speed button (Issue #519)? | Depends on whether the fan was already running. If the fan was off (or state unknown), it's treated as taking manual control — the SAME override machinery as a timer press. If the fan was ALREADY running, it's treated as a comfort-only speed adjustment — recorded via `handle_fan_speed_observed()`, which does NOT arm grace/HVAC-suppression. | [§ Speed-Press Classification](#speed-press-classification-issue-519) |
| What happens when one physical interaction touches both speed and timer? | The remote transmits them as separate packets moments apart (not simultaneously) — `coordinator.py` combines them into a short-lived burst and applies ONE decision, not two, once the combining window elapses. | [§ Burst Combining](#burst-combining-issue-519) |
| Is the timer absolute, or can a safety/comfort condition still turn the fan off? | Fully absolute (log-only) by design decision (2026-07-12). Every existing fan-off decision path is suppressed exactly as it is for any other manual fan override; a WARNING is logged instead of silently dropping the suppressed decision, so the behavior is observable in HA logs. | [§ Suppression Is Absolute](#suppression-is-absolute) |
| What happens to an active RF timer across an HA restart? | Nothing survives — it is not persisted. This matches CA's existing clean-slate policy for all override/grace state (Issue #327/#282); `restore_state()` resets `_fan_remote_timer_hours` (and, as of Issue #519, `_fan_remote_speed`) to `None` alongside `_fan_override_active`. | [§ Restart Behavior](#restart-behavior) |
| What clears an RF-timer-driven override? | The same two paths that clear any manual fan override: (1) the fan physically turns off (detected via `fan_entity`/`fan_state_entity`, routed to `on_fan_turned_off()`), or (2) the grace timer naturally expires (`_on_grace_expired()`). There is no separate "remote timer expired" detection — CA relies on the fan's own physical state. | [§ Clearing](#clearing) |
| What is out of scope for this feature? | Any code path that actually calls a speed-setting HA service (`fan.set_percentage` or similar) — this feature is detect-and-respect only, not detect-and-set. See [§ Scope](#scope) for the full boundary, including what changed from the original (pre-#519) scope. | [§ Scope](#scope) |
| Does an RF timer press also suppress HVAC? | Yes, as of Issue #495 — for `FAN_MODE_WHOLE_HOUSE`/`BOTH`, `handle_fan_manual_override()` schedules `_suppress_hvac_for_whf()`, the same helper `_activate_fan()` uses. Previously ONLY CA-initiated activation suppressed HVAC; a manual/remote fan-on left the AC armed for the life of the override. As of Issue #519, an override-classified speed press schedules the same suppression; a comfort-only speed observation does not. | [§ HVAC Suppression on Manual/Remote Fan-On](#hvac-suppression-on-manualremote-fan-on) |
| Can a stale/repeated remote event trigger a false override? | It used to. The `event.*` entity flaps to `unavailable` at arbitrary times (not just restart) and re-announces its STALE last `event_type` with the SAME state (the entity's `state` field IS the event timestamp). Issue #495 added a dedup guard (`_last_fan_remote_event_ts`) that ignores a re-announced identical timestamp — confirmed live: without it, a phantom 2h override fired with zero user action when the entity restored a 6-hour-stale `timer_2h`. | [§ Stale-Event Dedup](#stale-event-dedup) |
| Does the dashboard's remote-timer display stay accurate? | As of Issue #495, yes. Previously `handle_fan_manual_override()` unconditionally overwrote `_fan_remote_timer_hours` on every call — including plain non-remote re-stamps (e.g. the WHF fan entity re-reporting "on") — which nulled an active remote timer within seconds of a genuine press. Fixed by only overwriting when the caller is the remote itself (`is_remote_event=True`) or supplies a genuine value. `_fan_remote_speed` (Issue #519) uses the identical guarded-overwrite idiom. | [§ Timer Value Durability](#timer-value-durability) |
| How does CA find the ambient speed sensor without new config? | Auto-discovery via HA's entity/device registry, keyed off the already-configured `fan_remote_entity` — resolves its `device_id`, then scans sibling entities for a `sensor.*` matching an object-id hint. No new user-facing config field; a discovery miss (older firmware, or registry not yet populated at startup) degrades to "speed unknown," which IS the auto-detect + fallback mechanism. | [§ Ambient Speed Sensor Discovery](#ambient-speed-sensor-discovery-issue-519) |
| Can CA's OWN fan command be misread as a remote press? | It used to. The QuietCool device transmits AND receives on the same RF channel, so a CA-issued command can be heard back by this same receive-side entity within ~1-2 seconds. Issue #567 added an echo guard (`_is_recent_fan_command()`, the same 30s-window primitive `_async_fan_entity_changed()` already used) at the top of `_async_fan_remote_changed()` — event-context matching isn't available here since CA never calls a service on this receive-only entity. | [§ CA's Own Command Echo Suppression](#cas-own-command-echo-suppression-issue-567) |

---

## Scope

**Files:**
- `custom_components/climate_advisor/fan_status.py` — `parse_remote_timer_event()`, the single source of truth for the event-token → hours mapping; `parse_remote_speed_event()` (Issue #519), the sibling for the speed-token family
- `custom_components/climate_advisor/const.py` — `CONF_FAN_REMOTE_ENTITY`, `REMOTE_TIMER_EVENT_HOURS`; `REMOTE_SPEED_TOKENS`, `REMOTE_BURST_WINDOW_SECONDS`, `REMOTE_SPEED_SENSOR_OBJECT_ID_HINTS` (Issue #519)
- `custom_components/climate_advisor/coordinator.py` — subscription (`async_setup`) + `_async_fan_remote_changed()` dispatch handler; `_last_fan_remote_event_ts` (Issue #495 stale-event dedup); `_PendingFanRemoteBurst`, `_arm_fan_remote_burst()`/`_cancel_fan_remote_burst()`/`_flush_fan_remote_burst()` (Issue #519 burst combining + classification); `_resolve_fan_remote_speed_sensor()`/`_read_fan_remote_speed()` (Issue #519 ambient sensor discovery); `_is_recent_fan_command()` echo guard at ingestion (Issue #567)
- `custom_components/climate_advisor/ai_skills_context.py` — `_render_fan_cancel()` branches on the `fan_cancel` event's `trigger` field so CA's own drift-reconciliation self-correction (`physical_drift_correction`) doesn't render as "user turned off" (Issue #567)
- `custom_components/climate_advisor/automation.py` — `handle_fan_manual_override(duration_override=..., is_remote_event=..., remote_speed=...)`, `_start_grace_period(duration_override=...)`, the suppression WARNING at `_deactivate_fan()` and `fan_thermostat_check()`, `_suppress_hvac_for_whf()`/`_release_whf_and_reclassify()` (Issue #495 HVAC suppression + reclassify-on-exit); `handle_fan_speed_observed()` (Issue #519 comfort-only path)
- `custom_components/climate_advisor/api.py` + `custom_components/climate_advisor/frontend/index.html` — `fan_remote_speed` status field + dashboard display (Issue #519)
- `gunkl/quietcool-house-fan` `component.yaml` (separate repo) — the new `text_sensor.quietcool_speed` ambient entity (Issue #519)

**Out of scope for this spec** (see [grace-periods-spec.md](grace-periods-spec.md) for the general grace-period mechanics this feature reuses):
- Any code path that actually calls a speed-SETTING HA service (`fan.set_percentage` or similar) — Issue #519 is detect-and-respect only. `handle_fan_speed_observed()`, `_resolve_fan_remote_speed_sensor()`, and `_fan_remote_speed` are the seam a future CA-initiated speed-comfort feature would build on; `fan:`'s `on_speed_set` in the firmware already transmits the needed RF commands, so no firmware work would be needed for that future feature either.
- Explicit `on`/`off` event tokens for on/off DETECTION purposes — CA still relies on physical fan-entity state changes for that (see [§ Firmware Event Contract](#firmware-event-contract) for why); `on`/`off` remain relevant only as burst-combining context (see [§ Burst Combining](#burst-combining-issue-519)).
- Any change to the general manual-override/grace state machine — this feature only adds an optional duration override / speed value to the existing mechanism.

**Historical note:** prior to Issue #519, speed tokens (`low`/`medium`/`high`) were explicitly out of scope — "firmware decodes and emits these, CA does not act on them." That is no longer true; see [§ Speed-Press Classification](#speed-press-classification-issue-519) below for the current, in-scope behavior.

---

## Occupant Impact

Someone in the home presses "8 hours" on the QuietCool wall remote to run the whole-house
fan overnight. Without this feature, Climate Advisor has no way to know a timer was
selected — it only detects "the fan turned on," and after its own configured grace period
(default 30 minutes; commonly configured longer, e.g. 90 minutes) its automation can shut
the fan off, contradicting what the person just told the fan to do. With this feature, CA
hears the remote's timer selection and backs off for exactly that long instead.

---

## Firmware Event Contract

Source: [`gunkl/quietcool-house-fan`](https://github.com/gunkl/quietcool-house-fan) — an
ESPHome component for QuietCool whole-house attic fans (ESP32 + CC1101 radio). The fork
extends the upstream transmit-only component with **receive** capability: it decodes RF
packets from the physical wall remote and exposes them to Home Assistant as an **event
entity**.

- **Entity:** `event.quietcool_remote` (HA `event` platform — user-configured entity ID
  in CA via `fan_remote_entity`, see [§ Config](#config)).
- **On each fire:** the entity's *state* becomes the ISO timestamp of the fire; the decoded
  command is in `attributes["event_type"]`.
- **Edge-triggered:** the firmware's `on_packet` handler fires the event only when a
  decoded field's value *changes* from its last-held value — the remote periodically
  re-broadcasts a beacon with the same command, and duplicates are suppressed in firmware,
  not in CA.
- **Recognized `event_type` tokens** (RF command codes in parentheses):

  | Token | RF code | CA action |
  |---|---|---|
  | `timer_1h` | `0x91`/`0xA1`/`0xB1` (speed-context-dependent, see the [HIGH-speed timer fix PR](https://github.com/gunkl/quietcool-house-fan/pull/2)) | Accumulates into a burst; flushes as an override, grace = 3600 s |
  | `timer_2h` | `0x92`/`0xA2`/`0xB2` | Override, grace = 7200 s |
  | `timer_4h` | `0x94`/`0xA4`/`0xB4` | Override, grace = 14400 s |
  | `timer_8h` | `0x98`/`0xA8`/`0xB8` | Override, grace = 28800 s |
  | `timer_12h` | `0x9C`/`0xAC`/`0xBC` | Override, grace = 43200 s |
  | `timer_none` | `0x9F`/`0xAF`/`0xBF` | Override, grace = configured `manual_grace_seconds` |
  | `on` | `0xBF` | Not independently actionable — CA relies on physical fan-entity state for on/off detection instead. Only meaningful as burst-combining context (see [§ Burst Combining](#burst-combining-issue-519)) if it arrives while a speed/timer burst is already open. |
  | `off` | `0x80`/`0xB0` | Cancels any pending burst (see [§ Burst Combining](#burst-combining-issue-519)); on/off detection itself still flows through physical fan-entity state, not this token |
  | `low`/`medium`/`high` | `0x1F`/`0x2F`/`0x3F` | **As of Issue #519:** accumulates into a burst; flushes as either an override or a comfort-only observation — see [§ Speed-Press Classification](#speed-press-classification-issue-519) |

- **Known firmware guidance:** `off` is the only definitive power-down signal in the raw
  protocol; any other token confirms the fan is active. CA does not rely on this for
  power state — see the next point.

**Why CA doesn't act on the `on`/`off` tokens for power-state DETECTION:** because events are
edge-triggered, a bare power-on that doesn't also change the timer field may not emit any
token CA needs to react to, and a power-off might arrive out of order relative to the
physical fan entity's own state change. CA already has a robust, tested physical-state
detection path (`fan_entity`/`fan_state_entity` + `_async_fan_entity_changed()`) for on/off —
duplicating that logic against a second, less deterministic signal would be exactly the kind
of "sibling threshold drift" this codebase has been burned by before
(#400/#402/#417/#456/#458). This reasoning is unaffected by Issue #519 — `on`/`off` still do
not drive on/off detection; they matter only as burst-combining context now.

---

## Speed-Press Classification (Issue #519)

**Occupant impact:** if you adjust the QuietCool remote's speed dial while the fan is already
running for some other reason (nat-vent, a prior timer, another manual override), that's a
comfort preference — CA remembers it but doesn't treat it as "you just took manual control,"
so it doesn't arm a grace period or suppress HVAC on top of whatever was already happening. If
you select a speed while the fan was off, that IS taking manual control, exactly like pressing
a timer — CA backs off the same way it always has for a manual fan-on.

**Decision table** (implemented in `coordinator._flush_fan_remote_burst()`):

1. **Timer selected** (with or without speed) → always an **override**. An explicit timer
   press is always manual intent — unchanged from pre-#519 behavior.
2. **Bare speed press** (no timer) → consult whether the fan was already running **before**
   this interaction started:
   - Fan was OFF, or state unknown (command-only mode, no ground truth) → **override**. The
     "unknown" case defaults to override as the safe/conservative direction — never less
     protective than pre-#519 behavior (which ignored speed presses entirely).
   - Fan was ALREADY running → **comfort-only**. Routed to `handle_fan_speed_observed()`
     instead of `handle_fan_manual_override()` — records `_fan_remote_speed` and emits a
     `fan_speed_observed` event, but does NOT touch `_fan_override_active`, grace, or HVAC
     suppression.

**Correctness detail — why "was running" must be snapshotted at burst-OPEN time, not
flush time:** an earlier draft of this design planned to re-read `_get_fan_physical_state()`
at flush time, after any `on`-transition in the burst had already happened. Since the fan is
typically already on by the time the burst flushes (that's often the whole point of the
interaction), a flush-time read would answer "yes, already running" for nearly every case —
including genuine off→on overrides — silently misclassifying them as comfort-only. Fixed by
snapshotting `was_running_before` exactly once, the moment the burst opens (the first
speed/timer event of a new interaction), before anything in that interaction could have
changed the fan's state. Covered by a dedicated regression test
(`TestFanRemoteBurstClassification::test_timing_bug_regression_fan_turns_on_mid_burst_still_classifies_as_override`)
that was verified to fail against the flush-time-read version before the fix.

**Why a separate `handle_fan_speed_observed()` function, not a flag inside
`handle_fan_manual_override()`:** that function's entire contract is "arm an override" (sets
`_fan_override_active`, starts grace, suppresses HVAC). Smearing a "don't actually override"
path through it via a boolean flag would mix two different outcomes into one already-complex
function instead of keeping each single-purpose and independently testable — consistent with
this codebase's general aversion to conditional branches that change a function's entire
behavioral contract.

---

## Burst Combining (Issue #519)

A single physical interaction with the remote (e.g. selecting a speed AND a timer together)
transmits the two fields as **separate packets moments apart**, not simultaneously (see
`docs/remote-capture-protocol.md` in `gunkl/quietcool-house-fan`). Without combining, CA would
apply two independent decisions for what the user experienced as one action — two grace
periods, two HVAC-suppression schedules.

`coordinator.py` accumulates speed/timer events into a `_PendingFanRemoteBurst` and flushes
exactly once, `REMOTE_BURST_WINDOW_SECONDS` (default 1.5s) after the last related event:

- Each new speed/timer event within the window re-arms the timer (extends, doesn't
  double-flush) and overwrites the corresponding field — **latest value wins per field**. If
  the user selects a timer, then changes their mind and clears it (`timer_none`) within the
  window, the later confirmation correctly overwrites the accumulator before flush.
- `off` **cancels** the pending burst outright (does not flush it) — turning the fan off
  supersedes any not-yet-applied override/comfort intent.
- A bare `on` with no burst already open is not actionable (matches pre-#519 behavior); if it
  arrives while a burst IS open, it's accounted for as context but does not independently
  extend or open a new burst.

**Why 1.5 seconds, not longer:** grounded in the firmware's own documented protocol timing
(`docs/remote-capture-protocol.md`: `SAME_BURST_TOLERANCE_MS=400ms` per-value repeat spacing,
`CONFIRM_WINDOW_MS=1500ms` per-field confirm cycle, multi-field bursts observed arriving
within a similar few-second span) rather than an arbitrary "be safe" guess. A longer window
would only add latency before protection (grace/suppression) is armed when it should be, and
increases the risk of merging a genuinely separate, later user action into a stale prior
burst. **This value is provisional** pending live-hardware confirmation once the firmware
ships — flagged for tuning against real capture data, the same status as the firmware's own
`SELF_ECHO_WINDOW_MS`.

---

## Ambient Speed Sensor Discovery (Issue #519)

Firmware exposes a second entity, `text_sensor.quietcool_speed` (domain `sensor`, not
`event`), reporting the current speed as continuously-readable state — distinct from
`event.quietcool_remote`'s discrete presses. This exists for restart-survival and "the fan
came pre-set to a speed, never pressed this session" cases the press-event stream alone can't
cover, and as the seam a future CA-initiated speed-setting feature would read from.

**No new user-facing config.** `coordinator._resolve_fan_remote_speed_sensor()` auto-discovers
the sibling entity via HA's entity/device registry:

1. Resolve `fan_remote_entity` (already configured) → `device_id` via
   `entity_registry.async_get()`.
2. Scan sibling entities on that device via `entity_registry.async_entries_for_device()` for a
   `sensor.*` domain entity whose object_id matches `REMOTE_SPEED_SENSOR_OBJECT_ID_HINTS`
   (default: contains `"speed"`).
3. Cache the resolved entity_id once found (registry relationships don't change during a
   running session) — but **never cache a negative result**. HA's entity/device registry can
   populate asynchronously at startup, so a too-early miss must self-correct on a later call
   rather than permanently disabling the feature for the rest of the session.

`_read_fan_remote_speed()` is then a live, stateless value read each call — mirrors the
existing `_get_thermostat_capabilities()` precedent (a fresh capability/value read every call,
degrading to `None` on anything missing), NOT an accumulated "have we ever seen a speed event"
persisted boolean. This live-read-degrades-to-`None` behavior IS the auto-detect + fallback
mechanism: no sibling sensor (older/un-updated firmware) or an `unknown`/`unavailable` state
both resolve to `None`, identical to today's behavior for installs without this feature.

**Test infrastructure note:** this is the first feature in this codebase using HA's
entity/device registry. Building it surfaced a real gap in `tools/sim_harness/ha_stubs.py`:
`from homeassistant.helpers import entity_registry as er` resolves via the *parent* mock's
attribute, not `sys.modules[...]` directly, when the parent (`homeassistant.helpers`) is
itself a `MagicMock` — an auto-mocked attribute access returns a new, unrelated `MagicMock`,
not the actually-registered submodule. Fixed by pinning
`sys.modules["homeassistant.helpers"].entity_registry` (and `.device_registry`) to the real
registered submodule objects — the same fix already documented there for
`homeassistant.config_entries`.

**Dashboard display:** `api.py` exposes `fan_remote_speed` in the status payload (live read,
falling back to the engine's last press-derived `_fan_remote_speed` so the card isn't blank
between beacons); `index.html` shows it as an additional line on the WHF status card **only
when known** — omitted entirely (never "unknown speed") otherwise, per this project's existing
status-card conventions (`.status-item` extension, non-null-gated).

---

## Design — One Entry Point

**This section revises the original design in GitHub issue #486**, which proposed a
separate absolute predicate (`_user_fan_timer_holds()`) and a new engine method. Neither
was implemented. Instead:

1. **A remote timer press is a manual fan override that supplies its own grace duration.**
   `coordinator._async_fan_remote_changed()` parses the event and calls the SAME
   `automation.handle_fan_manual_override()` the physical-fan-on detection path already
   calls — passing an optional `duration_override` (seconds) and `remote_timer_hours`
   (for observability only).
2. **`_start_grace_period()`** gained a matching optional `duration_override` parameter.
   When set (and `source == "manual"`), it bypasses `desired_state.decide_grace_start()`'s
   normal resolution of `manual_grace_seconds` and uses the RF-supplied duration instead.
   `duration_override=None` (the case for `timer_none` and for the pre-existing
   physical-fan-on callsite) falls through to the configured default, unchanged.
3. **The token → hours mapping lives once**, in `const.REMOTE_TIMER_EVENT_HOURS`, parsed by
   the pure helper `fan_status.parse_remote_timer_event()`. No caller re-implements the
   mapping inline.
4. **Last-wins:** pressing a second timer while one is already active re-stamps the
   override and restarts the grace period at the new duration (same idempotency guarantee
   `handle_fan_manual_override()` already provided before this feature).

```
Remote press (event.quietcool_remote fires, event_type=timer_8h)
  → coordinator._async_fan_remote_changed()
      → fan_status.parse_remote_timer_event("timer_8h") -> (True, 8.0)
      → automation_engine.handle_fan_manual_override(duration_override=28800, remote_timer_hours=8.0)
          → _fan_override_active = True
          → _fan_remote_timer_hours = 8.0
          → _start_grace_period("manual", duration_override=28800)
              → grace expires in exactly 28800s, not the configured manual_grace_seconds
```

---

## HVAC Suppression on Manual/Remote Fan-On (Issue #495)

A remote timer press (or any manual fan-on detection) is a whole-house-fan-on event, and
WHF/AC mutual exclusion is a structural rule (see
[08-COMPUTATION-REFERENCE.md § Structural WHF/AC Mutual Exclusion](08-COMPUTATION-REFERENCE.md)),
not something specific to CA-initiated activation. Before Issue #495, only `_activate_fan()`
(the CA-initiated path) suppressed HVAC on WHF-on — `handle_fan_manual_override()` set the
override flag and started the grace timer, but never touched `_pre_fan_hvac_mode` or
`_set_hvac_mode`. A user manually turning on the fan (or pressing an RF timer) left the AC
armed for the entire override duration — up to 12 hours for a `timer_12h` press.

**Fix — reuse, don't duplicate:** `handle_fan_manual_override()` now schedules the same
`_suppress_hvac_for_whf()` helper `_activate_fan()` calls, scoped to `FAN_MODE_WHOLE_HOUSE`/
`BOTH` (never `FAN_MODE_HVAC` — the thermostat's own blower coexists with the compressor by
design). Because `handle_fan_manual_override()` is sync and `_suppress_hvac_for_whf()` is
async (it awaits `_set_hvac_mode()`), it is dispatched via `hass.async_create_task()` rather
than awaited directly.

**Exit is reclassify, not restore.** `_activate_fan()`'s counterpart, `_deactivate_fan()`,
restores the HVAC mode captured at activation time. That is appropriate for CA's own short
nat-vent cycles, but a manual/remote WHF session can run for hours — the captured mode is
often stale by exit (e.g. the session spans a sleep-setback transition). Ending a manual
session instead calls `_release_whf_and_reclassify()`, which releases `_pre_fan_hvac_mode`
and reuses the existing fan-off reassert path (`_async_reassert_setpoint_after_fan_off`,
Issue #359 Fix A) so the thermostat converges on CA's *current* classification. This fires
from `on_fan_turned_off()` (fan confirmed off by the triggering event) and
`clear_fan_override()` (grace expiry / user cancel) — the latter first checks the same
physical-fan-state ground truth `_reconcile_fan_physical_drift()` uses, and no-ops if the
fan is still running, so it doesn't race the post-grace fan reconcile.

---

## Suppression Is Absolute

Per the locked decision (2026-07-12), while an RF timer is active, hard comfort-floor and
safety-adjacent shutoff decisions are suppressed — logged, never overridden. This is
delivered by the **existing** override guard, not a new one:

- `_deactivate_fan()` already returns early when `_fan_override_active` is `True` — this is
  the choke point every CA-initiated fan-off funnels through (nat-vent exit, comfort-floor
  breach in both `check_natural_vent_conditions()` and `nat_vent_temperature_check()`,
  standard cycle-off, min-runtime cycle-off).
- `fan_thermostat_check()` has its own equivalent guard (it returns `"keep"` directly
  without ever reaching `_deactivate_fan()`), so it needs its own log line.

Both guards now check `_fan_remote_timer_hours is not None` and, when true, log a WARNING
(instead of the pre-existing INFO/DEBUG line used for a plain, non-RF manual override) —
so a suppressed automatic shutoff while a remote timer is active is visible in HA logs, not
silently dropped. No new predicate was added; a plain manual fan override (started by
physically toggling the fan, not via a remote timer) is unaffected and continues to log at
its pre-existing level.

---

## Restart Behavior

Consistent with CA's clean-slate policy for override/grace state (Issue #327/#282), an
active RF timer does **not** survive an HA restart:

- `_fan_remote_timer_hours` (and, as of Issue #519, `_fan_remote_speed`) are included in
  `get_serializable_state()` for observability only (dashboard/status display), never
  restored.
- `restore_state()` explicitly resets `_fan_remote_timer_hours = None` and
  `_fan_remote_speed = None` in the same clean-slate block that resets
  `_fan_override_active`/`_fan_override_time`/`_grace_active`. The burst-buffer state
  (`_fan_remote_burst`, Issue #519) is plain coordinator instance state, not persisted at all
  — it's cleared implicitly by process restart, with no explicit reset needed.
- After a restart, `reconcile_fan_on_startup()` (unchanged) decides the fan's disposition
  from physical state, the same as it always has.

**Incoming device-originated events are also suppressed during the restart window (Issue
#491).** The above covers CA's *own* override/grace state resetting cleanly — but the
QuietCool remote's underlying `event.*` entity can independently re-announce its last
retained `event_type` (e.g. a stale `timer_2h`) while HA is still settling right after
restart, as the ESPHome device reconnects. `_async_fan_remote_changed()` cannot tell that
apart from a fresh button press by inspecting the event alone, so it now calls
`_suppress_during_startup_coalescing()` before processing any timer token — the same
5-minute window `_async_thermostat_changed()` already used (Issue #321), now shared. A
real remote press in the first 5 minutes after a restart is not acted on during that
window — an accepted tradeoff, consistent with the existing thermostat-override behavior.

---

## CA's Own Command Echo Suppression (Issue #567)

**Occupant impact:** CA turns the fan on for a legitimate reason (e.g. resuming a nat-vent
cycle at the end of the night). Without this guard, that automation action could get
misread as the occupant pressing the physical remote, be relabeled "manual override" in the
Activity Report, and hand fan control away from CA for hours on the strength of a false
detection — precisely the confusion this section closes.

**Why this is possible:** the QuietCool ESPHome device is a single transceiver — the same
component that exposes CA's control entity also decodes RF traffic for `event.quietcool_remote`
(see [§ Firmware Event Contract](#firmware-event-contract): "extends the upstream
**transmit-only** component with **receive** capability"). A command CA transmits can be heard
back on the same channel and decoded indistinguishably from a real remote press — confirmed
live: a `nat_vent_cycling_on` command at `T` was followed 1.742 seconds later by a `low` speed
token on `event.quietcool_remote`, which the (unguarded) burst classifier treated as a fresh
press and armed a 3-hour manual override with zero actual user involvement (Issue #567).

**Fix:** `_async_fan_remote_changed()` now checks `_is_recent_fan_command(threshold_seconds=30.0)`
at ingestion, before any burst is opened — the same shared primitive
`_async_fan_entity_changed()` already used for the physical fan-entity path since Fix #239. See
`_is_recent_fan_command()`'s docstring in `coordinator.py` for the full list of guarded call
sites; this project has shipped the "a new fan-state listener forgot this guard" defect twice
(#417, #567), so any new listener should consult that list before assuming its own event source
is exempt.

**Why not `event.context` matching** (the *primary*, stronger signal
`_async_fan_entity_changed()` uses)? Structurally unavailable here: `event.quietcool_remote` is
receive-only — CA never calls an HA service on it, so HA never attaches a CA-issued `Context` to
its state-changed events. The time-window heuristic is the correct mechanism for this entity, not
a fallback shortcut.

**Tradeoff:** a genuine human remote press landing within 30 seconds of a CA-issued fan command
is silently dropped instead of acted on. This is the same tradeoff already accepted for the
physical-entity path since Fix #239 — timer/speed presses are deliberate, non-urgent actions, so
a 30s silent window is a small cost against a guaranteed-false override every time CA's own
command echoes back.

---

## Stale-Event Dedup (Issue #495)

The `unavailable`-during-restart flap above turned out to be a special case of a broader
problem: **the QuietCool `event.*` entity flaps to `unavailable` at arbitrary times, not
just at restart**, and restores its stale last `event_type` with the SAME `state` value
(the entity's `state` field IS the firmware event's own timestamp — e.g.
`"2026-07-13T03:48:40.960+00:00"`). The Issue #491 guard only covers the restart window;
outside it, nothing previously distinguished a genuine new press from a stale re-announce.

**Confirmed live:** a real install's remote entity flapped `unavailable`→restore six times
in one day at unrelated times (08:13, 08:46, 16:58, 17:40, 18:03, 19:05 — no restart
involved), each restoring a `timer_2h` state frozen from an earlier press at 06:41. At
16:58:02, this produced a `fan_manual_override(remote_timer_hours=2.0)` + a 2-hour grace
period with **zero user action** — CA's own fan control was spuriously suppressed for 2
hours, and because the grace re-stamps on every flap, a sufficiently flaky entity could
keep an override alive indefinitely.

**Fix:** the coordinator tracks `_last_fan_remote_event_ts` — the `state` (timestamp) of
the last event actually acted on. `_async_fan_remote_changed()` compares the incoming
`new_state.state` against it before doing anything else; an identical value is ignored
(DEBUG-logged) as a re-announce, not a fresh press. This generalizes the Issue #491
restart-only guard to every `unavailable`→restore flap, using the entity's own timestamp
rather than a time-window heuristic. Not persisted — a stale restore immediately after a
restart is already covered by `_suppress_during_startup_coalescing()`.

---

## Timer Value Durability (Issue #495)

`_fan_remote_timer_hours` (the value the dashboard's "remote timer: Xh" line reads) used to
get silently clobbered to `None` while a remote-timer override was still active.
`handle_fan_manual_override()` is the single shared entry point for BOTH remote-timer
presses AND plain non-remote fan-on detections (the WHF fan entity re-reporting `"on"`
after its own brief `unavailable` flap, or the thermostat's `fan_mode` attribute changing)
— and it unconditionally overwrote `_fan_remote_timer_hours = remote_timer_hours` on every
call. A non-remote re-stamp always passes `remote_timer_hours=None`, so it nulled an active
remote timer within seconds.

**Confirmed live:** querying the status API and the persisted engine state within seconds
of each other, during an active 8-hour RF timer override, showed the API returning
`fan_remote_timer_hours: null` while the persisted state still held `8.0` — the value was
oscillating, present only in the brief window between a remote press and the next
unrelated fan-entity re-detection.

**Fix:** `handle_fan_manual_override()` gained `is_remote_event: bool = False`. The stored
value is only overwritten when the call is a genuine remote event (`is_remote_event=True`
— covers both a real timer selection AND a deliberate `timer_none` "no timer" press, which
correctly clears the value), when a genuine non-`None` value is supplied, or when there was
no prior active override (the very first press, where `None` is the correct initial value).
A plain non-remote re-stamp of an already-active override now preserves whatever remote
timer was already recorded. `_async_fan_remote_changed()` passes `is_remote_event=True` on
every dispatch; the pre-existing physical-fan-on and thermostat `fan_mode` callers do not.

---

## Clearing

There is no dedicated "remote timer expired" detection. An RF-timer-driven override clears
via the same two paths as any other manual fan override:

1. **Physical fan-off** — when the QuietCool's own hardware timer completes (or the user
   powers off at the remote/thermostat), the physical fan entity transitions to off. If
   `fan_entity`/`fan_state_entity` is configured, `_async_fan_entity_changed()` detects
   this and routes to `on_fan_turned_off()`, which clears the override.
   **Dependency:** without a configured fan entity for physical-state detection, this path
   does not fire — the override only clears via grace expiry (below).
2. **Grace expiry** — `_on_grace_expired()` fires at the RF-supplied duration and clears
   the override through the existing three-branch expiry logic (see
   [grace-periods-spec.md § Timer Lifecycle](grace-periods-spec.md#timer-lifecycle)).

---

## Config

- `fan_remote_entity` (`CONF_FAN_REMOTE_ENTITY`) — optional HA `event` domain entity
  selector, in the same config-flow step (`sensors`) as the other fan fields. **Unset ⇒ no
  subscription is created ⇒ zero behavior change** from before this feature existed.
- No new default constants were added. `timer_none` and the pre-existing physical-fan-on
  path both continue to use the already-configurable `manual_grace_seconds`
  (`DEFAULT_MANUAL_GRACE_SECONDS = 1800`, i.e. 30 minutes).

---

## Code Reference

- [`parse_remote_timer_event`](../custom_components/climate_advisor/fan_status.py) — token → hours mapping (pure)
- [`parse_remote_speed_event`](../custom_components/climate_advisor/fan_status.py) — speed-token recognition (pure, Issue #519)
- [`REMOTE_TIMER_EVENT_HOURS`](../custom_components/climate_advisor/const.py) — the single-source mapping table
- [`REMOTE_SPEED_TOKENS`](../custom_components/climate_advisor/const.py), [`REMOTE_BURST_WINDOW_SECONDS`](../custom_components/climate_advisor/const.py), [`REMOTE_SPEED_SENSOR_OBJECT_ID_HINTS`](../custom_components/climate_advisor/const.py) — Issue #519
- [`_async_fan_remote_changed`](../custom_components/climate_advisor/coordinator.py) — event dispatch; `_last_fan_remote_event_ts` dedup guard (Issue #495); routes speed/timer into the burst accumulator (Issue #519)
- [`_arm_fan_remote_burst`/`_cancel_fan_remote_burst`/`_flush_fan_remote_burst`](../custom_components/climate_advisor/coordinator.py) — burst combining + override-vs-comfort classification (Issue #519)
- [`_resolve_fan_remote_speed_sensor`/`_read_fan_remote_speed`](../custom_components/climate_advisor/coordinator.py) — ambient sensor discovery via entity/device registry (Issue #519)
- [`handle_fan_manual_override`](../custom_components/climate_advisor/automation.py) — shared entry point (RF + physical paths); `is_remote_event` (Issue #495); `remote_speed` (Issue #519)
- [`handle_fan_speed_observed`](../custom_components/climate_advisor/automation.py) — comfort-only speed observation, does NOT arm override/grace/HVAC-suppression (Issue #519)
- [`_suppress_hvac_for_whf`](../custom_components/climate_advisor/automation.py) — shared HVAC-off helper, CA-initiated AND manual/remote (Issue #495)
- [`_release_whf_and_reclassify`](../custom_components/climate_advisor/automation.py) — manual-session exit: release + reclassify, not blind restore (Issue #495)
- [`_start_grace_period`](../custom_components/climate_advisor/automation.py) — `duration_override` resolution
- [`_deactivate_fan`](../custom_components/climate_advisor/automation.py) — primary suppression choke point + WARNING
- [`fan_thermostat_check`](../custom_components/climate_advisor/automation.py) — secondary suppression choke point + WARNING
- Tests: `tests/test_fan_remote.py` (incl. `TestParseRemoteSpeedEvent`, `TestFanRemoteBurstClassification`, `TestFanRemoteBurstWindow`, `TestFanRemoteSpeedSensorDiscovery`), `tests/test_whole_house_fan_hvac_suppression.py` (`TestManualWhfOnSuppressesHvac`, `TestManualWhfOffReleasesAndReclassifies`), `tests/test_api.py` (`TestStatusFanRemoteSpeed`)
