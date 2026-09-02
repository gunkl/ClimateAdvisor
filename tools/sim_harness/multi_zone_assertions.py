"""Multi-zone golden-scenario schema and assertion types (Issue #796).

See docs/multi-zone-spec.md, "Testing Without Multi-Zone Hardware" and
"Golden Scenario Schema Extension". This module is the harness-side
counterpart to ``build_headless_multi_zone()`` (build_coordinator.py): it
defines HOW a scenario file describes a multi-zone setup and HOW the three
new assertion types are evaluated against the zones that harness produces.

Deliberately NOT wired into ``tools/simulate.py``'s single-result execution
model (``run_scenario_production()`` / ``ClimateSimulator._check_assertion``)
— that model is built around one ``ProductionRunResult`` for one engine/
coordinator run. Driving a multi-zone scenario through ``simulate.py`` end to
end (new event types like ``unload_entry``, a "which zone does this event
target" dispatch layer, MANIFEST/report changes) is real, separate work for
whichever step actually authors the first zones-scenario — this module's job
is to hand that step a working, already-tested evaluator plus a stable schema
so it isn't designed twice. Both entry points below take the exact shape
``build_headless_multi_zone()`` returns (``zones: dict[str, dict]``,
``fake_hass: FakeHass``), not a scenario file — so calling them will not
change if/when the simulate.py wiring is added later.

Additive-only schema
---------------------
A golden/pending scenario file MAY carry an optional top-level ``"zones"``
array. Scenarios without this key are completely unaffected — this is a pure
extension, not a migration. Shape::

    {
      "zones": [
        {
          "zone_label": "zone_a",           # required, unique within the scenario
          "climate_entity": "climate.zone_a_thermostat",  # required
          "config": {...},                  # optional per-zone config overrides
          "events": [...]                    # optional, same event shape as the
                                              # existing single-zone "events" array
        },
        ...
      ],
      "assertions": [
        {"type": "cross_zone_isolation", ...},
        {"type": "service_registry_binding", ...},
        {"type": "teardown_cleanup", ...}
      ]
    }

Each ``zones[i]`` maps 1:1 to one real config entry that
``build_headless_multi_zone()`` sets up via the REAL ``async_setup_entry()``.
"""

from __future__ import annotations

from typing import Any

# The five DOMAIN-scoped services __init__.py's async_setup_entry() registers
# (Issue #796 confirmed via source read — see build_coordinator.py's
# build_headless_multi_zone() smoke-tested output). Hardcoded rather than
# imported from const.py because there is no single SERVICE_NAMES constant in
# production today — if one is added later, prefer that over this list.
KNOWN_SERVICE_NAMES = (
    "respond_to_suggestion",
    "force_reclassify",
    "resend_briefing",
    "dump_diagnostics",
    "reset_learning_data",
)

ASSERTION_TYPES = frozenset({"cross_zone_isolation", "service_registry_binding", "teardown_cleanup"})


# ---------------------------------------------------------------------------
# Schema validation (additive-only)
# ---------------------------------------------------------------------------


def validate_zones_schema(scenario: dict[str, Any]) -> None:
    """Validate the optional top-level ``"zones"`` array, if present.

    No-op (including for scenarios with no ``"zones"`` key at all) when the
    key is absent — this MUST never reject an existing single-zone scenario.
    Raises ``ValueError`` with a specific, actionable message on malformed
    input; never silently accepts a broken shape.
    """
    if "zones" not in scenario:
        return

    zones = scenario["zones"]
    if not isinstance(zones, list) or not zones:
        raise ValueError('"zones" must be a non-empty list when present')

    seen_labels: set[str] = set()
    for i, zone in enumerate(zones):
        if not isinstance(zone, dict):
            raise ValueError(f"zones[{i}] must be an object")
        label = zone.get("zone_label")
        if not label or not isinstance(label, str):
            raise ValueError(f'zones[{i}] must have a non-empty string "zone_label"')
        if label in seen_labels:
            raise ValueError(f'duplicate zone_label "{label}" in "zones"')
        seen_labels.add(label)
        if not zone.get("climate_entity"):
            raise ValueError(f'zones[{i}] ("{label}") must have "climate_entity"')
        if "config" in zone and not isinstance(zone["config"], dict):
            raise ValueError(f'zones[{i}] ("{label}") "config" must be an object')
        if "events" in zone and not isinstance(zone["events"], list):
            raise ValueError(f'zones[{i}] ("{label}") "events" must be a list')

    for a in scenario.get("assertions", []):
        a_type = a.get("type")
        if a_type not in ASSERTION_TYPES:
            continue  # not one of ours — the existing "expect"-based assertions are unaffected
        _validate_assertion_shape(a)


def _validate_assertion_shape(assertion: dict[str, Any]) -> None:
    a_type = assertion["type"]
    if a_type == "cross_zone_isolation":
        required = ("action_zone", "service", "unaffected_zone", "unaffected_field")
    elif a_type == "service_registry_binding":
        required = ("service", "expected_target_entry_id")
    elif a_type == "teardown_cleanup":
        required = ("unload_entry",)
    else:  # pragma: no cover — guarded by ASSERTION_TYPES membership above
        raise ValueError(f"unknown multi-zone assertion type {a_type!r}")
    missing = [key for key in required if key not in assertion]
    if missing:
        raise ValueError(f"{a_type} assertion missing required field(s): {missing}")


# ---------------------------------------------------------------------------
# Field resolution helper (dotted-path, e.g. "learning.thermal_model.k_passive")
# ---------------------------------------------------------------------------


class _FieldNotFound:
    """Sentinel — distinguishes 'field resolved to None' from 'field does not exist'."""


_FIELD_NOT_FOUND = _FieldNotFound()


def resolve_dotted_field(obj: Any, dotted_path: str) -> Any:
    """Walk a dotted attribute path off ``obj``, e.g. ``"learning.thermal_model.k_passive"``.

    Tries ``getattr`` first (production state lives on plain objects/
    dataclasses), then ``dict.__getitem__`` (a step may resolve to a plain
    dict, e.g. ``coordinator.config``). Returns ``_FIELD_NOT_FOUND`` — not
    ``None`` — if any step is missing, so a ``cross_zone_isolation`` check
    can distinguish "field genuinely became None" from "field path is wrong
    and this assertion is vacuously passing for the wrong reason".
    """
    current = obj
    for part in dotted_path.split("."):
        if current is _FIELD_NOT_FOUND:
            return _FIELD_NOT_FOUND
        if isinstance(current, dict):
            if part not in current:
                return _FIELD_NOT_FOUND
            current = current[part]
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            return _FIELD_NOT_FOUND
    return current


# ---------------------------------------------------------------------------
# Assertion evaluators
#
# All three take ``zones`` (the dict build_headless_multi_zone() returns) and
# ``fake_hass`` (the shared FakeHass), plus the assertion dict. Each returns
# ``(passed: bool, detail: str)`` — detail is always populated (pass or fail)
# so a scenario report can show *why*, not just pass/fail.
# ---------------------------------------------------------------------------


async def check_cross_zone_isolation(
    zones: dict[str, Any], fake_hass: Any, assertion: dict[str, Any]
) -> tuple[bool, str]:
    """Call a service, assert an unrelated zone's named field is unchanged.

    This is the literal Gap 5/9 bug class (docs/multi-zone-spec.md): a
    service call meant to affect one zone silently affects another because
    HA's service namespace is global, not per-config-entry, and the
    integration's handlers close over exactly one coordinator (whichever
    zone's ``async_setup_entry()`` ran last — see
    ``_FakeServices.async_register``'s last-write-wins docstring).

    ``action_zone`` is accepted for schema symmetry/forward-compatibility
    with a future per-zone-targeted service call (e.g. HA's ``target:
    {device_id: ...}`` service-call convention), but is NOT used to route the
    call today — production's service handlers take no zone-targeting
    parameter (confirmed: ``RESET_LEARNING_SCHEMA`` has no entry-id field).
    The assertion calls the named service exactly as a real user would
    (unscoped) and checks the OTHER zone's state — which is precisely what
    exposes the bug when it exists.

    ``unaffected_field`` must resolve to a value that actually changes when
    the service runs — a freshly-built coordinator's learning state has
    nothing populated, so e.g. ``"learning._state.dismissed_suggestions"``
    (confirmed real path — ``LearningEngine._state`` is a ``LearningState``
    instance) needs seeding first if used against a brand-new zone, same as
    ``test_sim_harness_multi_zone.py``'s ``test_cross_zone_isolation_detects_
    the_known_gap_5_bleed`` does. NOTE: ``learning.reset("all")`` *replaces*
    ``_state`` with a new ``LearningState()`` rather than mutating fields in
    place — since ``LearningState`` is a dataclass, ``==`` compares field
    values, not identity, so a field-value path (not the bare ``_state``
    object itself) is required for ``before == after`` to detect the change
    when the pre-reset state was otherwise all-defaults.
    """
    unaffected_zone = assertion["unaffected_zone"]
    field = assertion["unaffected_field"]
    service = assertion["service"]
    service_data = assertion.get("service_data", {})

    if unaffected_zone not in zones:
        return False, f'unaffected_zone "{unaffected_zone}" not in zones {list(zones)}'

    coordinator = zones[unaffected_zone]["coordinator"]
    before = resolve_dotted_field(coordinator, field)
    if before is _FIELD_NOT_FOUND:
        return False, f'field "{field}" does not resolve on zone "{unaffected_zone}" — check the dotted path'

    from custom_components.climate_advisor.const import DOMAIN  # noqa: PLC0415

    await fake_hass.services.async_call(DOMAIN, service, service_data)

    after = resolve_dotted_field(coordinator, field)
    passed = before == after
    detail = (
        f'zone "{unaffected_zone}" field "{field}": before={before!r} after={after!r}'
        f" (unaffected {'confirmed' if passed else 'VIOLATED — cross-zone bleed'})"
    )
    return passed, detail


def check_service_registry_binding(
    zones: dict[str, Any], fake_hass: Any, assertion: dict[str, Any]
) -> tuple[bool, str]:
    """Assert the currently-registered service handler is bound to the expected entry.

    Test-only closure introspection (see ``_FakeServices.get_handler``'s
    docstring for why this is a DIFFERENT, harness-only answer to a question
    real production diagnostics explicitly cannot answer via public HA APIs).
    Walks the handler closure's free variables for either ``coordinator`` or
    ``entry`` (production's ``handle_dump_diagnostics`` closes over ``entry``
    directly rather than ``coordinator`` — confirmed by source read — so both
    names are checked) and resolves the bound object's identity against the
    known zones.
    """
    from custom_components.climate_advisor.const import DOMAIN  # noqa: PLC0415

    service = assertion["service"]
    expected_entry_id = assertion["expected_target_entry_id"]

    handler = fake_hass.services.get_handler(DOMAIN, service)
    if handler is None:
        return False, f'service "{service}" is not registered'

    bound_entry_id = _resolve_bound_entry_id(handler, zones)
    if bound_entry_id is None:
        return False, f'could not determine which zone service "{service}" is bound to (closure inspection failed)'

    passed = bound_entry_id == expected_entry_id
    detail = f'service "{service}" is bound to entry "{bound_entry_id}" (expected "{expected_entry_id}")'
    return passed, detail


def _resolve_bound_entry_id(handler: Any, zones: dict[str, Any]) -> str | None:
    """Identity-match a handler closure's captured coordinator/entry against known zones."""
    code = getattr(handler, "__code__", None)
    closure = getattr(handler, "__closure__", None)
    if code is None or closure is None:
        return None

    captured: dict[str, Any] = dict(zip(code.co_freevars, (cell.cell_contents for cell in closure), strict=False))

    coordinator_by_id = {label: info["coordinator"] for label, info in zones.items()}
    entry_by_id = {label: info["entry"] for label, info in zones.items()}

    if "coordinator" in captured:
        bound_coordinator = captured["coordinator"]
        for label, coordinator in coordinator_by_id.items():
            if coordinator is bound_coordinator:
                return zones[label]["entry"].entry_id
    if "entry" in captured:
        bound_entry = captured["entry"]
        for entry in entry_by_id.values():
            if entry is bound_entry:
                return entry.entry_id
    return None


async def check_teardown_cleanup(zones: dict[str, Any], fake_hass: Any, assertion: dict[str, Any]) -> tuple[bool, str]:
    """Unload one zone, assert surviving zones' services/panel are still present.

    This is Gap 8/9's literal test: ``async_unload_entry()`` currently
    removes the dashboard panel and (via the global service registry) can
    leave dangling/incorrect service bindings unconditionally, without
    checking whether other zones are still active. Performs the unload
    itself (via the REAL ``async_unload_entry()``) — a teardown_cleanup
    assertion IS the unload event, not a check that runs after some other
    step already unloaded it, since no other event type currently drives an
    unload through the harness.
    """
    from custom_components.climate_advisor import async_unload_entry  # noqa: PLC0415

    unload_zone = assertion["unload_entry"]
    if unload_zone not in zones:
        return False, f'unload_entry "{unload_zone}" not in zones {list(zones)}'

    entry = zones[unload_zone]["entry"]
    await async_unload_entry(fake_hass, entry)

    checks: list[str] = []
    passed = True

    if "expect_services_present" in assertion:
        expected = bool(assertion["expect_services_present"])
        from custom_components.climate_advisor.const import DOMAIN  # noqa: PLC0415

        actually_present = any(fake_hass.services.has_service(DOMAIN, name) for name in KNOWN_SERVICE_NAMES)
        ok = actually_present == expected
        passed = passed and ok
        checks.append(f"services_present: expected={expected} actual={actually_present} ({'ok' if ok else 'FAIL'})")

    if "expect_panel_present" in assertion:
        expected = bool(assertion["expect_panel_present"])
        from custom_components.climate_advisor.const import PANEL_FRONTEND_PATH  # noqa: PLC0415

        actually_present = PANEL_FRONTEND_PATH in fake_hass.data.get("_panels", {})
        ok = actually_present == expected
        passed = passed and ok
        checks.append(f"panel_present: expected={expected} actual={actually_present} ({'ok' if ok else 'FAIL'})")

    return passed, f'after unloading "{unload_zone}": ' + "; ".join(checks)


async def check_multi_zone_assertion(
    zones: dict[str, Any], fake_hass: Any, assertion: dict[str, Any]
) -> tuple[bool, str]:
    """Dispatch to the right evaluator by ``assertion["type"]``.

    Returns ``(False, reason)`` for an assertion type this module does not
    own — callers should fall back to the existing single-result
    ``check_assertion()`` in outcomes.py for those, mirroring how that
    function itself signals "not applicable" via a ``False`` return.
    """
    a_type = assertion.get("type")
    if a_type == "cross_zone_isolation":
        return await check_cross_zone_isolation(zones, fake_hass, assertion)
    if a_type == "service_registry_binding":
        return check_service_registry_binding(zones, fake_hass, assertion)
    if a_type == "teardown_cleanup":
        return await check_teardown_cleanup(zones, fake_hass, assertion)
    return False, f"not a multi-zone assertion type: {a_type!r}"
