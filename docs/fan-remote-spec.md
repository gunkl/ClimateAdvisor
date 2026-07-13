<!-- Nav: ← [grace-periods-spec.md](grace-periods-spec.md) | → [automation.py](../custom_components/climate_advisor/automation.py) + [coordinator.py](../custom_components/climate_advisor/coordinator.py) + [fan_status.py](../custom_components/climate_advisor/fan_status.py) | ↔ [08-COMPUTATION-REFERENCE.md](08-COMPUTATION-REFERENCE.md) -->

# QuietCool RF Remote Timer Events — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does the occupant experience when they press a timer on the physical remote? | The whole-house fan runs for exactly the selected duration (1/2/4/8/12 hours) without Climate Advisor's own automation shutting it off partway through, the way it would with an un-communicated manual override. | [§ Occupant Impact](#occupant-impact) |
| What entity/attribute does the firmware expose, and what values does it emit? | An HA `event.*` entity (e.g. `event.quietcool_remote`). Each firmware-decoded remote press fires a state change with the command in `attributes["event_type"]`. Timer tokens: `timer_1h`, `timer_2h`, `timer_4h`, `timer_8h`, `timer_12h`, `timer_none`. | [§ Firmware Event Contract](#firmware-event-contract) |
| How does a timer selection map onto CA's existing grace mechanics? | It does NOT create a new predicate. A timer press calls the SAME `handle_fan_manual_override()` the physical-fan-on detection path already uses, with an optional `duration_override` (seconds) that bypasses the configured `manual_grace_seconds` for that one override. | [§ Design — One Entry Point](#design--one-entry-point) |
| Is the timer absolute, or can a safety/comfort condition still turn the fan off? | Fully absolute (log-only) by design decision (2026-07-12). Every existing fan-off decision path is suppressed exactly as it is for any other manual fan override; a WARNING is logged instead of silently dropping the suppressed decision, so the behavior is observable in HA logs. | [§ Suppression Is Absolute](#suppression-is-absolute) |
| What happens to an active RF timer across an HA restart? | Nothing survives — it is not persisted. This matches CA's existing clean-slate policy for all override/grace state (Issue #327/#282); `restore_state()` resets `_fan_remote_timer_hours` to `None` alongside `_fan_override_active`. | [§ Restart Behavior](#restart-behavior) |
| What clears an RF-timer-driven override? | The same two paths that clear any manual fan override: (1) the fan physically turns off (detected via `fan_entity`/`fan_state_entity`, routed to `on_fan_turned_off()`), or (2) the grace timer naturally expires (`_on_grace_expired()`). There is no separate "remote timer expired" detection — CA relies on the fan's own physical state. | [§ Clearing](#clearing) |
| What is out of scope for this feature? | Speed tokens (`low`/`medium`/`high`) and explicit `on`/`off` event handling are not decoded or acted on. Only the `timer_*` family drives behavior. | [§ Scope](#scope) |

---

## Scope

**Files:**
- `custom_components/climate_advisor/fan_status.py` — `parse_remote_timer_event()`, the single source of truth for the event-token → hours mapping
- `custom_components/climate_advisor/const.py` — `CONF_FAN_REMOTE_ENTITY`, `REMOTE_TIMER_EVENT_HOURS`
- `custom_components/climate_advisor/coordinator.py` — subscription (`async_setup`) + `_async_fan_remote_changed()` dispatch handler
- `custom_components/climate_advisor/automation.py` — `handle_fan_manual_override(duration_override=...)`, `_start_grace_period(duration_override=...)`, the suppression WARNING at `_deactivate_fan()` and `fan_thermostat_check()`

**Out of scope for this spec** (see [grace-periods-spec.md](grace-periods-spec.md) for the general grace-period mechanics this feature reuses):
- Speed tokens (`low`/`medium`/`high`) — firmware decodes and emits these, CA does not act on them this cut
- Explicit `on`/`off` event tokens — CA relies on physical fan-entity state changes for on/off detection instead (see [§ Firmware Event Contract](#firmware-event-contract) for why)
- Any change to the general manual-override/grace state machine — this feature only adds an optional duration override to the existing mechanism

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

  | Token | RF code | CA action this cut |
  |---|---|---|
  | `timer_1h` | `0x91` | Fan override, grace = 3600 s |
  | `timer_2h` | `0x92` | Fan override, grace = 7200 s |
  | `timer_4h` | `0x94` | Fan override, grace = 14400 s |
  | `timer_8h` | `0x98` | Fan override, grace = 28800 s |
  | `timer_12h` | `0x9C` | Fan override, grace = 43200 s |
  | `timer_none` | `0x9F` | Fan override, grace = configured `manual_grace_seconds` |
  | `on` | `0xBF` | Ignored this cut (see [§ Scope](#scope)) |
  | `off` | `0x80`/`0xB0` | Ignored this cut — CA relies on physical fan-entity state instead |
  | `low`/`medium`/`high` | `0x1F`/`0x2F`/`0x3F` | Ignored this cut |

- **Known firmware guidance:** `off` is the only definitive power-down signal in the raw
  protocol; any other token confirms the fan is active. CA does not rely on this for
  power state — see the next point.

**Why CA doesn't act on the `on`/`off` tokens:** because events are edge-triggered, a bare
power-on that doesn't also change the timer field may not emit any token CA needs to react
to, and a power-off might arrive out of order relative to the physical fan entity's own
state change. CA already has a robust, tested physical-state detection path
(`fan_entity`/`fan_state_entity` + `_async_fan_entity_changed()`) for on/off — duplicating
that logic against a second, less deterministic signal would be exactly the kind of
"sibling threshold drift" this codebase has been burned by before (#400/#402/#417/#456/#458).
The remote integration's sole job is to supply the **duration** when a timer is pressed;
everything else about fan on/off state continues to flow through the existing path.

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

- `_fan_remote_timer_hours` is included in `get_serializable_state()` for observability
  only (dashboard/status display), never restored.
- `restore_state()` explicitly resets `_fan_remote_timer_hours = None` in the same
  clean-slate block that resets `_fan_override_active`/`_fan_override_time`/`_grace_active`.
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
- [`REMOTE_TIMER_EVENT_HOURS`](../custom_components/climate_advisor/const.py) — the single-source mapping table
- [`_async_fan_remote_changed`](../custom_components/climate_advisor/coordinator.py) — event dispatch
- [`handle_fan_manual_override`](../custom_components/climate_advisor/automation.py) — shared entry point (RF + physical paths)
- [`_start_grace_period`](../custom_components/climate_advisor/automation.py) — `duration_override` resolution
- [`_deactivate_fan`](../custom_components/climate_advisor/automation.py) — primary suppression choke point + WARNING
- [`fan_thermostat_check`](../custom_components/climate_advisor/automation.py) — secondary suppression choke point + WARNING
- Tests: `tests/test_fan_remote.py`
