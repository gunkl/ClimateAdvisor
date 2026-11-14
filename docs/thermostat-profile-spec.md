# Thermostat Profile Spec (Issues #1566, #1556, #1567–#1569)

**Tier 3 spec.** Module: `custom_components/climate_advisor/thermostat_profile.py` (pure, no Home Assistant imports).
Consumers: `fan_mode_resolver.py` (every fan_mode read/write predicate), `automation.py` (`_fan_profile()`),
`coordinator.py` (`_fan_profile()`, `_tick_thermostat_profile()`, `thermostat_profile()`,
`thermostat_profile_diagnostics()`), `invariant_watchdog.py` (P5), `diagnostic_snapshot.py`, and the simulation
harness's promise probe.

## Occupant outcome

Climate Advisor works with whatever names a thermostat gives its fan modes, and nobody has to configure them. On a
Honeywell T6 Pro (Z-Wave JS, `fan_modes: ["Auto low", "Low", "Circulation"]`), "fan off" used to send `Low`, which is
**fan ON** on that thermostat. The blower then ran for days, and because `Auto low` read as "fan running" every
thermal observation was abandoned, so the model never left confidence `none` (#1556). Now "fan off" sends
`Auto low`, `Auto low` reads idle, and the thermostat's own reports confirm (or overrule) the meaning of each mode.

## Roles

Each fan-mode value gets one `FanRole`:

| Role | Meaning | Reads as blower running when… |
|---|---|---|
| `auto` | The blower runs only with a heat/cool call or the thermostat's own schedule | the thermostat reports `hvac_action == "fan"` |
| `off` | The blower is off | the thermostat reports `hvac_action == "fan"` |
| `circulate` | The thermostat runs the blower part of the time by itself | `hvac_action == "fan"`; with no reported action (`""`), always (conservative) |
| `on` | The blower runs continuously (`on`, `continuous`, or a named speed) | always |
| `unknown` | Nothing known | always (the pre-#1566 conservative read) |

**Name guess** (`classify_fan_mode_name`), used until there is evidence:

- `""` / `off` → off
- `auto*` → auto
- `circ*` → circulate
- `on`, `on …`, `continuous…`, `constant`, `always on` and the named speeds `low`/`medium`/`high`/`top`/`middle`/`max`/`min` → on
- anything else → unknown

## Evidence (Issue #1567)

The coordinator's 5-minute thermal tick credits the minutes since the previous tick (capped at 15 minutes) to the
**current** fan mode:

- `hvac_action == "fan"` → `fan_min`
- `hvac_action == "idle"` → `idle_min`
- anything else carries no fan-mode evidence: heating/cooling (the blower runs then whatever the mode), `off`
  (some thermostats report `off` while a continuous fan runs, which would make an ON mode look idle), and a missing
  value

Nothing is inferred from temperatures. This follows the owner rule that a run exists only when the thermostat
reports it.

A mode's observed role depends on its share of idle minutes:

| Evidence | Observed role |
|---|---|
| fewer than 30 minutes in total | none (the name guess stands) |
| idle share ≤ 5 % | `on` (continuous) |
| idle share ≥ 25 % | not continuous: the name role if it is auto/off/circulate, else `auto` |
| in between (e.g. an Ecobee fan-minimum-runtime) | none (the name guess stands) |

**Aging:** at most 1,440 minutes per mode. New minutes past the cap scale the older evidence down first, so old
evidence fades only as new evidence arrives. Nothing ages out by time alone, and nothing is ever wiped.

## Consistency rules (what CA may send)

`build_fan_profile()` picks:

- **off value:**
  1. a literal `auto` (with role auto);
  2. else another mode both named and resolved idle (`auto*`/`off`);
  3. else a mode *observed* not to run continuously;
  4. only when the list has **no idle-named mode at all** (e.g. a mini-split's `[low, medium, high]`): the lowest
     named speed. This is the #968 fallback, and it applies only while that speed is a name guess and differs from
     the on value. It is noted in `problems`.
- **on value:** a role-`on` mode in the #968 preference order (`on`, `high`, `medium`, `low`), else any other
  role-`on` mode.

**Guarantees** (property-tested, `tests/test_thermostat_profile_1566.py::TestConsistencyRules`):

- the on value always has role `on`;
- the off value is never circulate, unknown, or a mode observed running continuously;
- on ≠ off.

When there's no safe value, the result is `None`. The caller then sends nothing, and the coordinator logs
`Fan control held back` (WARNING).

`status` is one of:

- `verified`: every value sent was observed;
- `unverified`: a name guess, which is also how a thermostat without `hvac_action` stays;
- `blocked`: no off value;
- `no_fan_modes`.

## Persistence and hardware changes (Issue #1568)

`ThermostatProfileStore` is saved in the per-zone **state file** as `thermostat_profiles`, next to
`hvac_action_report`, and restored on every startup. A restart or a release never re-learns from scratch.

**Fingerprint:** `device:<device registry id>`, with the manufacturer and model from the device registry. Without a
device entry it falls back to `entity:<climate entity id>`. Both are read-only registry lookups.

| Change | Result | Log |
|---|---|---|
| first sight | new profile | INFO `Thermostat profile started` |
| same device, mode list changed (firmware) | evidence kept for unchanged modes, removed modes dropped, new modes start empty | INFO `Thermostat fan modes changed` |
| different device (replacement) | fresh profile; the previous one archived | WARNING `Thermostat changed … learning it fresh` |
| a device seen before (swap back) | its profile restored | WARNING `Thermostat changed … restored the profile learned for it earlier` |

Up to 5 archived thermostats are kept, most recently seen first.

## One profile (Issue #1569)

`coordinator.thermostat_profile()` returns a `ThermostatProfile` holding:

- the identity;
- the learned fan profile;
- the declared `ThermostatCapabilities` (heat/cool/dual setpoint). These are still read live through the engine's
  single accessor `_get_thermostat_capabilities()`, so they're composed here, not stored twice;
- `reports_runs`, the existing "has reported heating/cooling" record (`_hvac_report_watch`).

The engine reads the fan profile through `_fan_profile_provider`, the same live-provider pattern as #1230 R6b. A bare
engine (headless tools) uses the name guess.

## Observability

- **INFO `Thermostat profile: device=… hvac_modes=… fan: modes=[…] roles=[Low=on (observed fan=60m idle=0m), …] off='Auto low' on='Low' status=verified reports_hvac_action=True`**: logged once whenever a role, a sent value, a capability or a problem changes.
- **INFO `Thermostat reports hvac_action`**: the first time it does.
- **WARNING** lines for a device change and for `Fan control held back`.
- **Diagnostics** (`diagnostic_snapshot.thermostat_profile`, which also feeds the AI investigator footer): the device,
  capabilities, every mode's role, source, name guess and minutes, the sent values, status, problems and archived
  devices. Mode names, minutes and make/model only; no entity names or device ids.

## Household promises

The production command hook stamps `Command.fan_on` for `set_fan_mode` from the profile, and the harness probe stamps
it from the name guess. P5 (`no_blind_fan_start`) treats a thermostat fan start as `fan_on`, else the name guess. A
speed-valued fan start such as `Low` or `high` now counts, where before only a literal `"on"` did.

## Limits

- A thermostat that never reports `hvac_action` can't be verified by observation. It stays on the name guess, marked
  `unverified`.
- Probing (deliberately switching modes to learn them) is not done. Learning is passive, from modes CA or the
  occupant already set.
