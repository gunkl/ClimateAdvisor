"""Thermostat profile: what this thermostat's fan modes actually do (Issues #1566, #1556, #1567-#1569).

Home Assistant's ``fan_mode`` / ``fan_modes`` are integration-defined free text. A Honeywell T6 Pro on Z-Wave JS
reports ``["Auto low", "Low", "Circulation"]``, where ``Low`` is continuous ON (#1556). The old fixed English
preference lists sent ``Low`` for "fan off" and read ``Auto low`` as a running blower, so the blower latched on and
no thermal observation ever committed.

This module is the single place where a fan-mode value gets a meaning (a :class:`FanRole`):

1. **Name guess** (:func:`classify_fan_mode_name`). Used until there is evidence. ``auto*`` = automatic,
   ``circ*`` = circulate, ``off`` = off, and ``on`` / continuous / a named speed = blower running.
2. **Evidence** (:class:`FanModeEvidence`). Minutes the thermostat itself reported ``hvac_action == "fan"`` vs
   ``"idle"`` while that fan mode was set and heat/cool was not running. A mode that is almost never idle runs the
   blower continuously; a mode that is often idle does not. Observed evidence overrides the name. Nothing is
   inferred from temperatures ("no run without a reported action"): a thermostat that never reports
   ``hvac_action`` stays on the name guess and is reported as ``unverified``.
3. **Consistency rules** (:func:`build_fan_profile`). The off value is never a mode that runs the blower, and the
   on value always does. When no value is safe the profile says so (``None``) and the caller sends nothing.

Pure and dependency-free (no Home Assistant imports), so it is unit-testable and shared by ``automation.py``,
``coordinator.py``, ``fan_mode_resolver.py`` and the simulation harness. Persistence (one profile per thermostat
fingerprint, a bounded archive, restore on swap-back) is :class:`ThermostatProfileStore`, whose ``to_dict`` /
``from_dict`` round-trip the per-zone state file.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FanRole(StrEnum):
    """What a fan-mode value makes the thermostat's blower do."""

    AUTO = "auto"  # runs only with a heat/cool call (or the thermostat's own schedule)
    OFF = "off"  # blower off
    CIRCULATE = "circulate"  # the thermostat runs the blower part of the time on its own
    ON = "on"  # blower runs continuously (a literal "on", "continuous", or a named speed)
    UNKNOWN = "unknown"  # no meaning known: read as running (the conservative read, unchanged from #968)


# Roles whose blower runs only when the thermostat itself says so (hvac_action == "fan").
IDLE_ROLES: frozenset[FanRole] = frozenset({FanRole.AUTO, FanRole.OFF})

# Name-guess vocabulary (lower-cased, stripped). A named speed means "blower running at that speed", both on
# mini-splits ([auto, low, medium, high]) and on thermostats whose only "on" is a speed (the T6 Pro's "Low").
_ON_NAMES: frozenset[str] = frozenset(
    {"on", "continuous", "constant", "always on", "high", "medium", "low", "top", "middle", "max", "min"}
)
_OFF_NAMES: frozenset[str] = frozenset({"", "off"})

# Preference among blower-running modes when CA turns its fan ON -- unchanged from Issue #968, so every thermostat
# that already worked picks the same value: a literal "on", else the highest named speed.
_ON_PREFERENCE: tuple[str, ...] = ("on", "high", "medium", "low")

# Evidence thresholds. Minutes are thermostat-reported, sampled on the coordinator's 5-minute thermal tick.
EVIDENCE_MIN_MINUTES = 30.0  # below this a mode has no observed role yet
EVIDENCE_CAP_MINUTES = 1440.0  # per mode; older evidence is scaled down as new minutes arrive (ages out by evidence)
ON_MAX_IDLE_SHARE = 0.05  # idle share at or below this: the blower runs continuously in this mode
NOT_ON_MIN_IDLE_SHARE = 0.25  # idle share at or above this: the blower does not run continuously in this mode


def _norm(value: Any) -> str:
    return str(value if value is not None else "").strip().lower()


def classify_fan_mode_name(value: Any) -> FanRole:
    """The name guess for one fan-mode value (no evidence)."""
    name = _norm(value)
    if name in _OFF_NAMES:
        return FanRole.OFF
    if name.startswith("auto"):
        return FanRole.AUTO
    if name.startswith("circ"):
        return FanRole.CIRCULATE
    if name in _ON_NAMES or name.startswith(("on ", "continuous")):
        return FanRole.ON
    return FanRole.UNKNOWN


@dataclass
class FanModeEvidence:
    """Thermostat-reported minutes for one fan mode while heat/cool was not running."""

    fan_min: float = 0.0
    idle_min: float = 0.0

    @property
    def total_min(self) -> float:
        return self.fan_min + self.idle_min

    @property
    def idle_share(self) -> float | None:
        return self.idle_min / self.total_min if self.total_min > 0 else None

    def add(self, *, fan: bool, minutes: float) -> None:
        """Add ``minutes`` of evidence. Past the cap, older evidence is scaled down first (it ages out by evidence)."""
        if minutes <= 0:
            return
        minutes = min(minutes, EVIDENCE_CAP_MINUTES)
        total = self.total_min
        if total > 0 and total + minutes > EVIDENCE_CAP_MINUTES:
            keep = max(0.0, EVIDENCE_CAP_MINUTES - minutes) / total
            self.fan_min *= keep
            self.idle_min *= keep
        if fan:
            self.fan_min += minutes
        else:
            self.idle_min += minutes

    def observed_runs_blower(self) -> bool | None:
        """True: observed continuous. False: observed not continuous. None: too little evidence, or in between."""
        share = self.idle_share
        if share is None or self.total_min < EVIDENCE_MIN_MINUTES:
            return None
        if share <= ON_MAX_IDLE_SHARE:
            return True
        if share >= NOT_ON_MIN_IDLE_SHARE:
            return False
        return None

    def to_dict(self) -> dict[str, float]:
        return {"fan_min": round(self.fan_min, 2), "idle_min": round(self.idle_min, 2)}

    @classmethod
    def from_dict(cls, data: Any) -> FanModeEvidence:
        if not isinstance(data, dict):
            return cls()
        try:
            return cls(
                fan_min=max(0.0, float(data.get("fan_min", 0.0))), idle_min=max(0.0, float(data.get("idle_min", 0.0)))
            )
        except TypeError, ValueError:
            return cls()


@dataclass(frozen=True)
class FanModeInfo:
    """One fan mode's resolved role and where it came from."""

    mode: str
    role: FanRole
    source: str  # "observed" | "name"
    name_role: FanRole
    fan_min: float = 0.0
    idle_min: float = 0.0


@dataclass(frozen=True)
class FanProfile:
    """The resolved meaning of every fan mode the thermostat offers, plus the values CA may send."""

    modes: tuple[str, ...]
    info: dict[str, FanModeInfo]
    off_value: str | None
    on_value: str | None
    reports_hvac_action: bool = False
    problems: tuple[str, ...] = ()

    def role_of(self, value: Any) -> FanRole:
        """The role of a fan_mode value read from the entity (a value outside the list gets the name guess)."""
        key = _norm(value)
        for mode, info in self.info.items():
            if _norm(mode) == key:
                return info.role
        return classify_fan_mode_name(value)

    @property
    def status(self) -> str:
        """``verified`` (every value CA sends was observed), ``unverified`` (a name guess), or ``blocked``."""
        if not self.modes:
            return "no_fan_modes"
        if self.off_value is None:
            return "blocked"
        used = [v for v in (self.off_value, self.on_value) if v is not None]
        return "verified" if all(self.info[v].source == "observed" for v in used) else "unverified"

    def signature(self) -> tuple[Any, ...]:
        """Changes only when a role, its source, a sent value or a problem changes (drives the INFO line)."""
        return (
            self.modes,
            tuple((m, self.info[m].role.value, self.info[m].source) for m in self.modes),
            self.off_value,
            self.on_value,
            self.reports_hvac_action,
            self.problems,
        )

    def describe(self) -> str:
        """One-line, grep-friendly summary (mode names and minutes only; nothing personal)."""
        parts = []
        for mode in self.modes:
            i = self.info[mode]
            ev = f" fan={i.fan_min:.0f}m idle={i.idle_min:.0f}m" if i.fan_min or i.idle_min else ""
            parts.append(f"{mode}={i.role.value} ({i.source}{ev})")
        problems = f" problems={list(self.problems)}" if self.problems else ""
        return (
            f"modes={list(self.modes)} roles=[{', '.join(parts)}] off={self.off_value!r} on={self.on_value!r}"
            f" status={self.status} reports_hvac_action={self.reports_hvac_action}{problems}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "modes": list(self.modes),
            "roles": {
                m: {
                    "role": self.info[m].role.value,
                    "source": self.info[m].source,
                    "name_guess": self.info[m].name_role.value,
                    "fan_min": round(self.info[m].fan_min, 1),
                    "idle_min": round(self.info[m].idle_min, 1),
                }
                for m in self.modes
            },
            "off_value": self.off_value,
            "on_value": self.on_value,
            "status": self.status,
            "reports_hvac_action": self.reports_hvac_action,
            "problems": list(self.problems),
        }


def _resolve_role(name_role: FanRole, observed: bool | None) -> tuple[FanRole, str]:
    if observed is None:
        return name_role, "name"
    if observed:
        return FanRole.ON, "observed"
    # Observed NOT continuous: keep a name that already says so; anything else behaves like auto.
    if name_role in (FanRole.AUTO, FanRole.OFF, FanRole.CIRCULATE):
        return name_role, "observed"
    return FanRole.AUTO, "observed"


def build_fan_profile(
    fan_modes: Iterable[Any] | None,
    evidence: dict[str, FanModeEvidence] | None = None,
    *,
    reports_hvac_action: bool = False,
) -> FanProfile:
    """Resolve every offered fan mode's role and pick the on/off values under the consistency rules.

    Off value: a literal ``auto`` (auto role), then another mode both named and resolved idle (``auto*``/``off``),
    then a mode OBSERVED not to run the blower, and only when there is no idle mode at all, the lowest named speed
    (the #968 fallback) unless it was observed running continuously. A circulate, unknown or observed-continuous
    mode is never the off value, so turning the fan "off" never picks a known fan-ON value over an auto (#1556).
    On value: a mode whose role is ON, in the
    Issue #968 preference order, then any other ON mode. Either may be ``None``; the caller then sends nothing.
    """
    modes = tuple(str(m) for m in (fan_modes or ()) if m is not None)
    evidence = evidence or {}
    info: dict[str, FanModeInfo] = {}
    for mode in modes:
        name_role = classify_fan_mode_name(mode)
        ev = evidence.get(mode) or FanModeEvidence()
        role, source = _resolve_role(name_role, ev.observed_runs_blower())
        info[mode] = FanModeInfo(mode, role, source, name_role, ev.fan_min, ev.idle_min)

    problems: list[str] = []
    for mode in modes:
        i = info[mode]
        if i.source == "observed" and i.name_role in IDLE_ROLES and i.role is FanRole.ON:
            problems.append(f"{mode!r} is named like an idle mode but the thermostat reports its blower running")

    def first(pred: Callable[[FanModeInfo], bool]) -> str | None:
        return next((m for m in modes if pred(info[m])), None)

    off_value = (
        first(lambda i: _norm(i.mode) == "auto" and i.role is FanRole.AUTO)
        or first(lambda i: i.role in IDLE_ROLES and i.name_role in IDLE_ROLES)
        or first(lambda i: i.role in IDLE_ROLES and i.source == "observed")
    )
    on_value = None
    for want in _ON_PREFERENCE:
        on_value = first(lambda i, w=want: _norm(i.mode) == w and i.role is FanRole.ON)
        if on_value:
            break
    on_value = on_value or first(lambda i: i.role is FanRole.ON)
    if off_value is None and modes and not any(info[m].name_role in IDLE_ROLES for m in modes):
        # No idle mode at all (e.g. a mini-split's [low, medium, high]): the LOWEST named speed offered is the
        # least-on choice (the Issue #968 fallback) -- only while it is a name guess (never one the thermostat was
        # observed running continuously, #1556) and only when it is not also the fan-on value.
        lowest = next(
            (m for want in reversed(_ON_PREFERENCE[1:]) for m in modes if _norm(m) == want),
            None,
        )
        if lowest is not None and info[lowest].source == "name" and lowest != on_value:
            off_value = lowest
            problems.append(f"no idle fan mode: fan off sends the lowest speed {off_value!r}")
    if modes and off_value is None:
        problems.append("no fan mode that leaves the blower idle")
    if modes and on_value is None:
        problems.append("no fan mode that runs the blower continuously")
    return FanProfile(modes, info, off_value, on_value, reports_hvac_action, tuple(problems))


def role_of(value: Any, profile: Any = None) -> FanRole:
    """``value``'s role in ``profile`` when it is a real :class:`FanProfile`, else the name guess."""
    return profile.role_of(value) if isinstance(profile, FanProfile) else classify_fan_mode_name(value)


def fan_mode_runs_blower(value: Any, hvac_action: Any, profile: FanProfile | None = None) -> bool:
    """True if the thermostat's own attributes say its blower is physically running.

    An idle-role mode (auto/off) runs the blower only when the thermostat reports ``hvac_action == "fan"``. A
    circulate mode runs it part of the time: a reported ``hvac_action`` decides, and with none (``""``) it reads as
    running (conservative, as before #1566). ON and UNKNOWN read as running.
    """
    role = role_of(value, profile)
    action = _norm(hvac_action)
    if role in IDLE_ROLES:
        return action == "fan"
    if role is FanRole.CIRCULATE:
        return True if action == "" else action == "fan"
    return True


def fan_command_starts_blower(value: Any, profile: FanProfile | None = None) -> bool:
    """True if commanding ``fan_mode = value`` turns the blower on continuously (household-promise journal)."""
    role = role_of(value, profile)
    return role is FanRole.ON


def evidence_kind(hvac_action: Any) -> bool | None:
    """``True`` = blower-only report, ``False`` = idle report, ``None`` = no fan-mode evidence in this reading.

    Heating/cooling (and every other value) carries none: the blower runs then whatever the fan mode. ``"off"`` is
    deliberately not idle evidence -- some thermostats report ``off`` with the system off even while a continuous
    fan runs, which would make an ON mode look idle and could make it the off value.
    """
    action = _norm(hvac_action)
    if action == "fan":
        return True
    if action == "idle":
        return False
    return None


# ── Persistence: one profile per thermostat fingerprint (Issue #1568) ──────────────────────────────────────

PROFILE_ARCHIVE_MAX = 5  # archived (not active) thermostats kept, most recently seen first


@dataclass
class StoredThermostat:
    """What CA has learned about one physical thermostat."""

    fingerprint: str
    manufacturer: str | None = None
    model: str | None = None
    fan_modes: list[str] = field(default_factory=list)
    evidence: dict[str, FanModeEvidence] = field(default_factory=dict)
    reports_hvac_action: bool = False
    last_seen: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "fan_modes": list(self.fan_modes),
            "evidence": {m: e.to_dict() for m, e in self.evidence.items()},
            "reports_hvac_action": self.reports_hvac_action,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, data: Any) -> StoredThermostat | None:
        if not isinstance(data, dict) or not isinstance(data.get("fingerprint"), str):
            return None
        ev = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
        modes = data.get("fan_modes") if isinstance(data.get("fan_modes"), list) else []

        def _s(key: str) -> str | None:
            return data.get(key) if isinstance(data.get(key), str) else None

        return cls(
            fingerprint=data["fingerprint"],
            manufacturer=_s("manufacturer"),
            model=_s("model"),
            fan_modes=[str(m) for m in modes],
            evidence={str(m): FanModeEvidence.from_dict(e) for m, e in ev.items()},
            reports_hvac_action=bool(data.get("reports_hvac_action", False)),
            last_seen=_s("last_seen"),
        )


@dataclass(frozen=True)
class ThermostatIdentity:
    """Who the configured thermostat is: the device registry entry, or the entity id when there is no device."""

    fingerprint: str
    manufacturer: str | None = None
    model: str | None = None

    @property
    def label(self) -> str:
        return " ".join(p for p in (self.manufacturer, self.model) if p) or "unknown device"


@dataclass(frozen=True)
class StoreChange:
    """What :meth:`ThermostatProfileStore.select` changed, for the caller's log lines."""

    kind: str  # "none" | "new" | "restored" | "swapped" | "modes_changed"
    previous: StoredThermostat | None = None
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


class ThermostatProfileStore:
    """Profiles per thermostat fingerprint: the active one and a bounded archive of the others."""

    def __init__(self) -> None:
        self._profiles: dict[str, StoredThermostat] = {}
        self._active: str | None = None

    @property
    def active(self) -> StoredThermostat | None:
        return self._profiles.get(self._active) if self._active else None

    def archived(self) -> list[StoredThermostat]:
        return [p for k, p in self._profiles.items() if k != self._active]

    def select(self, identity: ThermostatIdentity, fan_modes: Iterable[Any] | None, now_iso: str) -> StoreChange:
        """Make ``identity`` the active thermostat and reconcile its offered fan-mode list.

        A new fingerprint starts a fresh profile ("new" on first use, "swapped" when it replaces another); a
        fingerprint seen before is "restored" with its evidence. On the same fingerprint, evidence is kept for modes
        still offered, removed modes are dropped, and new ones start with none ("modes_changed").
        """
        modes = [str(m) for m in (fan_modes or ()) if m is not None]
        previous = self.active
        kind = "none"
        if self._active != identity.fingerprint:
            if identity.fingerprint in self._profiles:
                kind = "restored"
            else:
                self._profiles[identity.fingerprint] = StoredThermostat(
                    identity.fingerprint, identity.manufacturer, identity.model, list(modes)
                )
                kind = "new" if previous is None else "swapped"
            self._active = identity.fingerprint
        cur = self._profiles[identity.fingerprint]
        cur.manufacturer = identity.manufacturer or cur.manufacturer
        cur.model = identity.model or cur.model
        cur.last_seen = now_iso
        added: tuple[str, ...] = ()
        removed: tuple[str, ...] = ()
        if modes and modes != cur.fan_modes:
            added = tuple(m for m in modes if m not in cur.fan_modes)
            removed = tuple(m for m in cur.fan_modes if m not in modes)
            for m in removed:
                cur.evidence.pop(m, None)
            if kind == "none" and cur.fan_modes and (added or removed):
                kind = "modes_changed"
            cur.fan_modes = list(modes)
        self._prune()
        return StoreChange(kind, previous if kind in ("swapped", "restored") else None, added, removed)

    def record(self, fan_mode: Any, *, fan: bool, minutes: float) -> None:
        """Add thermostat-reported evidence for the active thermostat's current fan mode."""
        cur = self.active
        if cur is None or fan_mode is None:
            return
        mode = str(fan_mode)
        if cur.fan_modes and mode not in cur.fan_modes:
            return  # a value outside the advertised list: nothing to learn for the list
        cur.evidence.setdefault(mode, FanModeEvidence()).add(fan=fan, minutes=minutes)

    def note_reports_hvac_action(self) -> bool:
        """Mark that the active thermostat reports hvac_action; True the first time."""
        cur = self.active
        if cur is None or cur.reports_hvac_action:
            return False
        cur.reports_hvac_action = True
        return True

    def fan_profile(self, fan_modes: Iterable[Any] | None) -> FanProfile:
        cur = self.active
        return build_fan_profile(
            fan_modes,
            cur.evidence if cur is not None else None,
            reports_hvac_action=bool(cur is not None and cur.reports_hvac_action),
        )

    def _prune(self) -> None:
        others = sorted(self.archived(), key=lambda p: p.last_seen or "", reverse=True)
        for stale in others[PROFILE_ARCHIVE_MAX:]:
            self._profiles.pop(stale.fingerprint, None)

    def to_dict(self) -> dict[str, Any]:
        return {"active": self._active, "profiles": [p.to_dict() for p in self._profiles.values()]}

    def load(self, data: Any) -> None:
        """Replace this store's contents with a saved ``to_dict`` payload, in place (malformed parts are skipped)."""
        self._profiles = {}
        self._active = None
        if not isinstance(data, dict):
            return
        raw_profiles = data.get("profiles")
        for raw in raw_profiles if isinstance(raw_profiles, list) else []:
            p = StoredThermostat.from_dict(raw)
            if p is not None:
                self._profiles[p.fingerprint] = p
        active = data.get("active")
        self._active = active if isinstance(active, str) and active in self._profiles else None
        self._prune()

    @classmethod
    def from_dict(cls, data: Any) -> ThermostatProfileStore:
        store = cls()
        store.load(data)
        return store


def live_fan_profile(store: ThermostatProfileStore | None, state: Any) -> FanProfile:
    """The fan profile for a climate entity's live state: learned evidence from ``store`` when there is one.

    ``state`` is a Home Assistant ``State`` (or ``None``). A missing entity or a non-list ``fan_modes`` gives an
    empty profile, and an empty profile makes :func:`fan_mode_resolver.resolve_fan_mode_command` fall back to a
    name-only profile of the list its caller passes.
    """
    attrs = getattr(state, "attributes", None)
    modes = attrs.get("fan_modes") if isinstance(attrs, dict) else None
    if not isinstance(modes, (list, tuple)):
        modes = None
    if isinstance(store, ThermostatProfileStore):
        return store.fan_profile(modes)
    return build_fan_profile(modes)


@dataclass(frozen=True)
class ThermostatProfile:
    """Everything Climate Advisor knows about the configured thermostat, in one place (Issue #1569).

    ``capabilities`` is the engine's ``ThermostatCapabilities`` (heat/cool/dual setpoint): declared by the entity
    and read live by ``AutomationEngine._get_thermostat_capabilities()``, its single accessor, so it is composed
    here, not stored. ``reports_runs`` is the coordinator's existing "has reported heating/cooling" record. The fan
    profile is the learned part.
    """

    identity: ThermostatIdentity
    fan: FanProfile
    capabilities: Any
    reports_runs: bool

    def _caps(self) -> tuple[Any, ...]:
        c = self.capabilities
        return tuple(
            getattr(c, k, None)
            for k in ("modes", "supports_heat", "supports_cool", "supports_heat_cool", "supports_dual_setpoint")
        )

    def signature(self) -> tuple[Any, ...]:
        return (self.identity.fingerprint, self.fan.signature(), self._caps(), self.reports_runs)

    def describe(self) -> str:
        modes, heat, cool, heat_cool, dual = self._caps()
        return (
            f"device={self.identity.label} hvac_modes={list(modes or ())} heat={heat} cool={cool}"
            f" heat_cool={heat_cool} dual_setpoint={dual} reports_runs={self.reports_runs} fan: {self.fan.describe()}"
        )

    def as_dict(self) -> dict[str, Any]:
        modes, heat, cool, heat_cool, dual = self._caps()
        return {
            "device": self.identity.label,
            "capabilities": {
                "hvac_modes": list(modes or ()),
                "supports_heat": heat,
                "supports_cool": cool,
                "supports_heat_cool": heat_cool,
                "supports_dual_setpoint": dual,
            },
            "reports_runs": self.reports_runs,
            "fan": self.fan.as_dict(),
        }
