"""Fan-status suppression predicate for Climate Advisor.

``ClimateAdvisorCoordinator._compute_fan_status()`` returns one of seven string
values (see CLAUDE.md's "Fan Status Values" table). Multiple call sites need to
answer the same question — "is the thermostat reporting fan activity that CA's
own fan control already explains, or is this a genuine contradiction worth a
warning?" — and each has historically hand-rolled its own allow-list of "active"
values (Issue #458, following the same "sibling threshold drift" pattern as
#400/#402/#417/#456). This module is the single source of truth for that
predicate, mirroring the existing ``temperature.py`` precedent: a tiny,
dependency-free utility module imported by both ``coordinator.py`` and
``ai_skills_activity.py``.
"""

from __future__ import annotations

from .const import REMOTE_SPEED_TOKENS, REMOTE_TIMER_EVENT_HOURS, is_unavailable_state

FAN_STATUS_ACTIVE_VALUES: frozenset[str] = frozenset(
    {
        "active",
        "active (unconfirmed)",
        "running (manual override)",
        "running (untracked)",
        "running (thermostat)",
    }
)

# Observed (not CA-controlled) thermostat-schedule blower: one story via fan_observed_* events, not an anomaly.
FAN_STATUS_THERMOSTAT = "running (thermostat)"


def is_ca_fan_running(fan_status: str) -> bool:
    """True if `fan_status` represents fan activity CA can account for.

    `fan_status` is the return value of ``_compute_fan_status()``. Used to
    suppress a false "state contradiction" warning when the thermostat reports
    `hvac_action="fan"` while `hvac_mode="off"` — expected whenever CA itself
    activated fan-only mode (natural ventilation, manual override, or an
    untracked-but-real fan run), not a genuine data inconsistency.

    Must include all four non-inactive, non-disabled active values from the
    "Fan Status Values" table — omitting any one (as `ai_skills_activity.py`
    omitted `"active (unconfirmed)"` before Issue #458) causes a fan legitimately
    in that state to be misreported as a contradiction.
    """
    return fan_status in FAN_STATUS_ACTIVE_VALUES


def fan_entity_on(state) -> bool | None:
    """Whether a fan/switch entity's state reads "on": True / False, or None when unknowable.

    Issue #1050 (#1048 change 21): the single reading of a fan command entity's state, shared
    by ``ClimateAdvisorCoordinator._get_fan_physical_state()``'s command-entity fallback and
    ``AutomationEngine``'s command read-back (``_reconcile_fan_physical_drift``).

    None means "cannot tell" and callers must NOT treat it as off:
      - ``state`` is None (entity missing), or its state is ``unavailable``/``unknown``
        (previously the fallback read those as off, which could strand or falsely release a
        session);
      - the entity reports ``assumed_state`` (an RF/IR/momentary transmitter whose HA state
        only echoes the last command and carries no feedback).
    Otherwise ``"on"``/``"true"`` (any case) is True and every other state is False.
    """
    if state is None:
        return None
    raw = getattr(state, "state", None)
    if not isinstance(raw, str) or is_unavailable_state(raw):
        return None
    attrs = getattr(state, "attributes", None)
    if isinstance(attrs, dict) and attrs.get("assumed_state"):
        return None
    return raw.lower() in ("on", "true")


def resolve_untracked_fan_status(*, recent_fan_command: bool, idle_status: str = "inactive") -> str:
    """Resolve the ground-truth 'physical signal says on, CA owns nothing' fallback
    to 'running (untracked)' or `idle_status` (Issue #571).

    Call only once the caller has already confirmed the raw signal (WHF physical
    entity state, or thermostat fan_mode/hvac_action) looks like "on" and every CA
    ownership flag that would otherwise explain it (_fan_active, _fan_override_active,
    and — for the ground-truth-fallback call sites — _natural_vent_active) is False.
    If CA very recently issued its own off-command, the physical signal simply
    hasn't caught up yet — that's the expected propagation window, not a real
    untracked fan (the OFF-direction mirror of the "active (unconfirmed)" guard
    these same call sites already apply in the ON direction). Shared by
    _compute_fan_status()/_compute_whf_status()/_compute_hvac_fan_status() so the
    three don't drift out of sync again (see Issue #510's history of needing
    parallel fixes across the first two).

    `idle_status` (Issue #955) lets a caller whose "not yet confirmed off" state
    isn't literally "inactive" supply its own correct fallback — e.g. a nat-vent
    session mid-cycle-off (`_natural_vent_active` still True) should settle back to
    "nat-vent (session active, fan idle)", not "inactive", during the guard window.
    Defaults to "inactive" for the original ground-truth-fallback call sites, where
    every ownership flag including _natural_vent_active is already False.
    """
    return idle_status if recent_fan_command else "running (untracked)"


def parse_remote_timer_event(event_type: str | None) -> tuple[bool, float | None]:
    """Parse a QuietCool RF remote ``event_type`` token into a timer decision.

    `event_type` is read from an ``event.*`` entity's
    ``attributes["event_type"]`` (see docs/fan-remote-spec.md for the firmware
    contract — ``gunkl/quietcool-house-fan``). This is the single source of
    truth for the token-to-hours mapping (``const.REMOTE_TIMER_EVENT_HOURS``);
    callers must not re-implement the mapping inline (Issue #486, following the
    "sibling threshold drift" lesson from #400/#402/#417/#456/#458).

    Returns ``(is_timer_event, hours)``:
    - Recognized timer token (``timer_1h``..``timer_12h``) -> ``(True, <hours>)``
    - ``timer_none`` -> ``(True, None)`` (use CA's configured grace duration)
    - Any other token (``on``, ``off``, speed tokens, unknown, ``None``) ->
      ``(False, None)`` — not a timer token; caller should check
      ``parse_remote_speed_event()`` next (Issue #519) before giving up on it.
    """
    if event_type not in REMOTE_TIMER_EVENT_HOURS:
        return False, None
    return True, REMOTE_TIMER_EVENT_HOURS[event_type]


def parse_remote_speed_event(event_type: str | None) -> str | None:
    """Parse a QuietCool RF remote ``event_type`` token into a speed selection (Issue #519).

    Mirrors ``parse_remote_timer_event()``'s shape for this module's other token family —
    the firmware already emits ``low``/``medium``/``high`` on an explicit speed-select
    press; this is the single place that recognizes them, so no caller re-implements the
    token set inline (the same "sibling threshold drift" concern documented above).

    Returns the speed token verbatim (``"low"``/``"medium"``/``"high"``) if `event_type` is
    a speed press, else ``None``.
    """
    return event_type if event_type in REMOTE_SPEED_TOKENS else None


def remote_event_is_echo_of(direction: str | None, event_type: str | None) -> bool:
    """Whether an RF remote ``event_type`` can be the device echoing Climate Advisor's own last command (Issue #1215).

    The QuietCool receiver hears its own transmitter (Issue #567), so a CA command can come back as a remote event
    seconds later. The echo is always the SAME kind of event as the command: CA's ``turn_off`` is echoed as ``off``
    and nothing else, so a ``timer_*`` / speed / ``on`` token seconds after CA's off is the occupant at the remote
    (2026-11-01 15:07: a ``timer_1h`` press 22 s after CA's stop was dropped as an "echo" and he got the 180-minute
    default). CA's ``turn_on`` is echoed as the device re-announcing the running state (``on``, a speed such as the
    15:04:16.5 ``low``, or its default timer), so every token except ``off`` stays suppressed there; an ``off`` right
    after CA's start is the occupant stopping it.

    ``direction`` is ``"on"``/``"off"`` for a recent WHF control command, ``None`` when unknown (a thermostat blower
    command, or none recent): everything stays a possible echo, the pre-#1215 behaviour.
    """
    if direction == "off":
        return event_type == "off"
    if direction in ("on", "cycle"):
        # "cycle" is the forced start (off now, on 5 s later): its echoes are on-side tokens too, and an occupant
        # ``off`` pressed during it must never be dropped (the second step then also aborts).
        return event_type != "off"
    return True


def fan_transition_is_echo_of(direction: str | None, is_on: bool) -> bool:
    """Whether a fan entity turning on/off can be the lagging echo of Climate Advisor's own last command (Issue #1215).

    The sibling of ``remote_event_is_echo_of`` for the physical-entity listener. The power reading lags a command by
    seconds (5.5 s on, 15.5 s off), so a transition the SAME way as CA's last command is that command arriving; a
    transition the OTHER way (the fan turning ON seconds after CA's stop) cannot be, it is the occupant. ``None`` or
    any other value (unknown, or a forced-start cycle whose first step is an off): every transition stays a possible
    echo, the pre-#1215 behaviour.
    """
    if direction == "on":
        return is_on
    if direction == "off":
        return not is_on
    return True


def is_dual_entity_transmitter_event(
    fan_entity: str | None, state_entity: str | None, feedback_enabled: bool, event_entity_id: object
) -> bool:
    """True when a fan state change is the control (transmitter) entity of a dual-entity WHF install (Issue #1215).

    Dual-entity = a ``fan_state_entity`` distinct from the ``fan_entity`` with ``fan_state_feedback`` on. On such an
    install the transmitter is not a witness of the occupant (the firmware flips it by itself), so occupant on/off
    detection reads the power entity and the RF remote events only. Single-entity and command-only installs are
    never dual-entity.
    """
    if not (fan_entity and state_entity and state_entity != fan_entity and feedback_enabled):
        return False
    return event_entity_id == fan_entity
