"""Generic cross-lifecycle event dispatcher for Block 5's FSM migration (epic
#594, Issue #633).

Replaces the alternative this plan's first draft considered and rejected —
hand-forwarding an emitted event at each ``automation.py`` call site that
happens to touch two lifecycles. That alternative reproduces the exact "forgot
to wire one site" bug class Issue #615 and #631 each independently hit (a
scattered mirror-write, and a scattered field-sync, respectively): every call
site touching 2+ lifecycles would carry its own translation code, easy to add
a new lifecycle to and easy to silently miss a site for.

This module holds no lifecycle-specific logic — it is a small, generic routing
registry, in the same spirit as ``coordinator.py``'s ``_mirror_to_shadow()``/
``_sync_shadow_inputs()`` being one audited choke point instead of N per-site
copies. A controller registers which event types it may emit and which it
consumes; ``LifecycleDispatcher.emit()`` routes an event to every registered
consumer of its type. ``check_registry_completeness()`` is the durable
enforcement mechanism — the same shape as ``tests/test_shadow_engine_coverage.py``'s
AST-registry test — asserting every declared-emittable event type has at least
one registered consumer, so a controller that's supposed to react to something
but was never wired up fails loudly instead of silently.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .lifecycle_events import LifecycleEvent, LifecycleEventType

_LOGGER = logging.getLogger(__name__)

# A consumer callback receives the emitted event and does whatever its own
# controller needs (typically: feed it into that controller's handle()).
EventConsumer = Callable[[LifecycleEvent], None]


@dataclass
class _Registration:
    name: str
    emits: frozenset[LifecycleEventType]
    consumes: frozenset[LifecycleEventType]
    on_event: EventConsumer | None


@dataclass
class LifecycleDispatcher:
    """Routes ``LifecycleEvent``s between registered lifecycle controllers.

    Deliberately synchronous and side-effect-free beyond calling the
    registered consumer callbacks — it does not itself touch HA, dry_run, or
    any actuation path. Isolation for shadow-only use continues to come from
    which controllers/callbacks are wired in at construction time (same
    precedent as ``AutomationEngineCallbacks``, Issue #604/N2), not from
    anything in this class.
    """

    _registrations: dict[str, _Registration] = field(default_factory=dict)
    _log: list[LifecycleEvent] = field(default_factory=list)

    def register(
        self,
        name: str,
        *,
        emits: frozenset[LifecycleEventType] = frozenset(),
        consumes: frozenset[LifecycleEventType] = frozenset(),
        on_event: EventConsumer | None = None,
    ) -> None:
        """Register a controller's emit/consume contract.

        ``on_event`` is required if ``consumes`` is non-empty (there'd be
        nothing to route to otherwise) and ignored if ``consumes`` is empty.
        """
        if consumes and on_event is None:
            raise ValueError(f"controller {name!r} declares consumes={consumes} but no on_event callback")
        self._registrations[name] = _Registration(name=name, emits=emits, consumes=consumes, on_event=on_event)

    def emit(self, event: LifecycleEvent) -> None:
        """Route an event to every controller registered as a consumer of its type.

        Logged unconditionally (audit trail by construction, epic success
        criterion #2) before routing. A consumer callback raising is logged
        and swallowed — one controller's bug must never block delivery to
        the others, same isolation posture as ``_mirror_to_shadow()``.
        """
        self._log.append(event)
        for reg in self._registrations.values():
            if event.event_type not in reg.consumes:
                continue
            assert reg.on_event is not None  # enforced at register() time
            try:
                reg.on_event(event)
            except Exception:  # noqa: BLE001 — one consumer's bug must not block others
                _LOGGER.exception(
                    "Lifecycle dispatcher: consumer %r raised handling %s from %s",
                    reg.name,
                    event.event_type,
                    event.source,
                )

    @property
    def event_log(self) -> list[LifecycleEvent]:
        """Every event routed so far, in order — the cross-lifecycle audit trail."""
        return list(self._log)

    def check_registry_completeness(self) -> list[str]:
        """Return a list of problems: every emittable event type with zero
        registered consumers. Empty list means the registry is complete.

        This is deliberately a plain method (not an assertion) so it can be
        driven either from a unit test (```assert dispatcher.check_registry_completeness() == []```)
        or, later, from a live coordinator health check — same "check, don't
        just claim" posture as the shadow-engine coverage registry.
        """
        all_emitted: set[LifecycleEventType] = set()
        all_consumed: set[LifecycleEventType] = set()
        for reg in self._registrations.values():
            all_emitted |= reg.emits
            all_consumed |= reg.consumes

        problems = []
        for event_type in sorted(all_emitted, key=lambda e: e.value):
            if event_type not in all_consumed:
                problems.append(f"{event_type.value} is declared emittable but has no registered consumer")
        return problems
