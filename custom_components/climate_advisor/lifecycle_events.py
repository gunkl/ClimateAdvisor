"""Cross-lifecycle event vocabulary for Block 5's FSM migration (epic #594, Issue #633).

A small, closed set of events one lifecycle's transition can emit and another's
can consume, routed through ``lifecycle_dispatcher.LifecycleDispatcher`` rather
than by lifecycles calling into each other's code or by scattered per-call-site
forwarding (see the plan's tabulated comparison for why the latter was rejected
— it reproduces the same "forgot to wire one site" bug class as Issue #615/#631,
just relocated).

Extended only when a new, concrete coupling is found (the original 6 below are
each justified by a real finding — #584's Finding 3, or the #631 investigation;
the nat-vent session pair added for Issue #717 is justified by
door_window_fsm.py's own documented cross-read of natural_vent_active/
whf_owns_hvac), never speculatively for a lifecycle whose actual needs aren't
known yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class LifecycleEventType(Enum):
    """Every event type a controller may emit or consume via the dispatcher."""

    GRACE_STARTED = "grace_started"
    GRACE_ENDED = "grace_ended"
    DOOR_PAUSE_STARTED = "door_pause_started"
    DOOR_PAUSE_ENDED = "door_pause_ended"
    OVERRIDE_CONFIRMED = "override_confirmed"
    OVERRIDE_CLEARED = "override_cleared"
    # Issue #717: door_window_fsm.py's own inputs read natural_vent_active/
    # whf_owns_hvac directly off the engine — nat-vent has no single real
    # activation chokepoint the way door/window and override/grace do (it's
    # written at ~18 scattered call sites), so this pair is emitted from a
    # before/after diff wrapped around _decision_pass() (the one point every
    # nat-vent-mutating entry point already funnels through under
    # _decision_lock) rather than at each individual write site.
    NAT_VENT_SESSION_STARTED = "nat_vent_session_started"
    NAT_VENT_SESSION_ENDED = "nat_vent_session_ended"


@dataclass(frozen=True)
class LifecycleEvent:
    """One emitted event — the payload a consuming controller's ``handle()`` sees.

    ``source`` is the emitting controller's name (e.g. ``"override_grace"``),
    used only for the dispatcher's audit log (epic success criterion #2) — never
    branched on by a consumer, which should react to the event *type* alone.
    """

    event_type: LifecycleEventType
    source: str
    at: datetime
    detail: str | None = None
