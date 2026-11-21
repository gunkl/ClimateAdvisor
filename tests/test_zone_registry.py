"""Tests for zone_registry.py (Issue #796 Gap 4).

Covers get_coordinator/iter_coordinators/get_default_coordinator across
0/1/2+ zones, and get_default_coordinator's deterministic-first-entry
fallback (via hass.config_entries.async_entries(DOMAIN)'s stable order, NOT
dict-iteration order) plus its WARNING log when more than one zone is
loaded — the "Transitional Safety Window" behavior from
docs/multi-zone-spec.md.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.climate_advisor import zone_registry
from tools.sim_harness.ha_stubs import ConfigEntry, ConfigEntryState


def _make_hass(coordinators: dict[str, MagicMock], ordered_entry_ids: list[str] | None = None) -> MagicMock:
    """Build a fake hass whose config entries are the zones (Issue #1639: ``entry.runtime_data``).

    ``ordered_entry_ids``, when given, is the config entries' stable registration order
    (``async_entries()``); only the ids in ``coordinators`` are LOADED, each with its coordinator as
    ``runtime_data``, so an id listed only in ``ordered_entry_ids`` models an entry that is registered but
    still setting up (or unloaded). ``coordinators``' own dict order never drives a result, so tests can
    prove the stable order is the one used. Without ``ordered_entry_ids`` the order is ``coordinators``'.
    """
    hass = MagicMock()
    hass.data = {}
    order = list(coordinators) if ordered_entry_ids is None else list(ordered_entry_ids)
    entries = []
    for entry_id in order:
        entry = ConfigEntry(entry_id=entry_id, title=entry_id)
        if entry_id in coordinators:
            entry.runtime_data = coordinators[entry_id]
            entry.state = ConfigEntryState.LOADED
        entries.append(entry)
    hass.config_entries.async_entries = MagicMock(return_value=entries)
    hass.config_entries.async_loaded_entries = MagicMock(
        side_effect=lambda domain=None: [e for e in entries if e.state == ConfigEntryState.LOADED]
    )
    return hass


def _unload(hass: MagicMock, entry_id: str) -> None:
    """Unload one zone the way HA does: its entry leaves LOADED and loses its ``runtime_data``."""
    for entry in hass.config_entries.async_entries():
        if entry.entry_id == entry_id:
            del entry.runtime_data
            entry.state = ConfigEntryState.NOT_LOADED


class TestGetCoordinator:
    def test_returns_coordinator_for_known_entry_id(self):
        coord = MagicMock()
        hass = _make_hass({"entry_a": coord})
        assert zone_registry.get_coordinator(hass, "entry_a") is coord

    def test_returns_none_for_unknown_entry_id(self):
        hass = _make_hass({"entry_a": MagicMock()})
        assert zone_registry.get_coordinator(hass, "entry_zzz") is None

    def test_returns_none_when_domain_not_present(self):
        hass = MagicMock()
        hass.data = {}
        assert zone_registry.get_coordinator(hass, "entry_a") is None

    def test_returns_none_when_zero_zones_loaded(self):
        hass = _make_hass({})
        assert zone_registry.get_coordinator(hass, "entry_a") is None


class TestIterCoordinators:
    def test_yields_all_loaded_coordinators(self):
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass({"entry_a": coord_a, "entry_b": coord_b})
        assert set(zone_registry.iter_coordinators(hass)) == {coord_a, coord_b}

    def test_empty_when_zero_zones_loaded(self):
        hass = _make_hass({})
        assert list(zone_registry.iter_coordinators(hass)) == []

    def test_empty_when_domain_not_present(self):
        hass = MagicMock()
        hass.data = {}
        assert list(zone_registry.iter_coordinators(hass)) == []


class TestGetDefaultCoordinatorZeroOrOneZone:
    def test_returns_none_when_zero_zones_loaded(self):
        hass = _make_hass({})
        assert zone_registry.get_default_coordinator(hass) is None

    def test_returns_none_when_domain_not_present(self):
        hass = MagicMock()
        hass.data = {}
        assert zone_registry.get_default_coordinator(hass) is None

    def test_single_zone_convenience_path(self):
        """Exactly one zone loaded: single-zone behavior is unchanged (backward-compat)."""
        coord = MagicMock()
        hass = _make_hass({"entry_a": coord})
        assert zone_registry.get_default_coordinator(hass) is coord

    def test_single_zone_path_does_not_consult_config_entries(self):
        """The single-zone convenience path must not need async_entries() at all."""
        coord = MagicMock()
        hass = _make_hass({"entry_a": coord})
        zone_registry.get_default_coordinator(hass)
        hass.config_entries.async_entries.assert_not_called()


class TestGetDefaultCoordinatorMultiZoneFallback:
    def test_deterministic_first_entry_by_config_entries_order(self):
        """Fallback picks the FIRST entry per async_entries()'s stable order.

        entry_b is listed first in the coordinators dict (so plain dict
        iteration order would favor it) but async_entries() reports entry_a
        first — the fallback must follow async_entries(), not dict order.
        """
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_b": coord_b, "entry_a": coord_a},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        assert zone_registry.get_default_coordinator(hass) is coord_a

    def test_fallback_follows_config_entries_order_reversed(self):
        """Same zones, opposite async_entries() order — proves it's not incidentally matching dict order."""
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_b", "entry_a"],
        )
        assert zone_registry.get_default_coordinator(hass) is coord_b

    def test_fallback_logs_warning(self, caplog):
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"):
            zone_registry.get_default_coordinator(hass)
        assert any("Multiple Climate Advisor zones" in rec.message for rec in caplog.records)
        assert any("entry_a" in rec.message for rec in caplog.records)

    def test_three_zones_picks_first_of_three(self):
        coord_a, coord_b, coord_c = MagicMock(), MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_c": coord_c, "entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_b", "entry_c", "entry_a"],
        )
        assert zone_registry.get_default_coordinator(hass) is coord_b


class TestGetDefaultCoordinatorWarningThrottle:
    """Blocking rejection fix: the ambiguous-zone WARNING must not fire on every call —
    it fully evicted log_capture.py's 200-entry ring buffer roughly every 40s under the
    dashboard's 60s/5-endpoint polling (no entry_id, no PR9 zone selector yet)."""

    def test_repeated_calls_with_same_outcome_log_once(self, caplog):
        """Simulates repeated dashboard polls resolving to the same fallback zone —
        must log exactly once, not once per call."""
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"):
            for _ in range(10):
                result = zone_registry.get_default_coordinator(hass)
                assert result is coord_a

        ambiguous_records = [rec for rec in caplog.records if "Multiple Climate Advisor zones" in rec.message]
        assert len(ambiguous_records) == 1

    def test_no_time_dependency_many_rapid_calls_still_log_once(self, caplog):
        """The throttle must not depend on a live clock — proven by calling far more times
        than any plausible poll interval would produce, with no sleep/time mocking at all."""
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"):
            for _ in range(500):
                zone_registry.get_default_coordinator(hass)

        ambiguous_records = [rec for rec in caplog.records if "Multiple Climate Advisor zones" in rec.message]
        assert len(ambiguous_records) == 1

    def test_different_resolved_entry_logs_again(self, caplog):
        """A DIFFERENT outcome (the resolved fallback zone changed) is new information and
        must still log — the throttle suppresses repeats of the SAME outcome only.

        Uses three zones so the second call still exercises the multi-zone branch after
        entry_a unloads (two zones remain — entry_b, entry_c — not the single-zone path)."""
        coord_a, coord_b, coord_c = MagicMock(), MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b, "entry_c": coord_c},
            ordered_entry_ids=["entry_a", "entry_b", "entry_c"],
        )
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"):
            zone_registry.get_default_coordinator(hass)
            # entry_a unloads mid-run (e.g. that zone's config entry removed) —
            # the ordered-entries loop now resolves to entry_b instead. Two
            # zones (b, c) still remain, so this stays on the multi-zone branch.
            _unload(hass, "entry_a")
            zone_registry.get_default_coordinator(hass)

        ambiguous_records = [rec for rec in caplog.records if "Multiple Climate Advisor zones" in rec.message]
        assert len(ambiguous_records) == 2
        assert "entry_a" in ambiguous_records[0].message
        assert "entry_b" in ambiguous_records[1].message

    def test_reset_warning_state_allows_relogging_same_outcome(self, caplog):
        """reset_warning_state() (called by __init__.py's async_unload_entry() once zone
        count drops back to <= 1) clears the throttle so a later recurrence warns again."""
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"):
            zone_registry.get_default_coordinator(hass)
            zone_registry.reset_warning_state(hass)
            zone_registry.get_default_coordinator(hass)

        ambiguous_records = [rec for rec in caplog.records if "Multiple Climate Advisor zones" in rec.message]
        assert len(ambiguous_records) == 2

    def test_reset_warning_state_is_noop_when_nothing_warned_yet(self):
        """Must not raise when called before any warning has ever been logged (e.g. a
        zone unload happening while only ever a single zone was ever loaded)."""
        hass = _make_hass({"entry_a": MagicMock()})
        zone_registry.reset_warning_state(hass)  # should not raise

    def test_skips_config_entries_not_yet_loaded(self):
        """async_entries() may list an entry mid-setup, before it is LOADED with its runtime_data —
        the fallback must skip past it to the next entry that IS actually loaded."""
        coord_a = MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a},
            ordered_entry_ids=["entry_pending", "entry_a"],
        )
        # One zone is loaded here, so this actually exercises the
        # single-zone convenience path — add a second loaded zone to force
        # the multi-zone branch while still including an unloaded entry.
        coord_b = MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_pending", "entry_a", "entry_b"],
        )
        assert zone_registry.get_default_coordinator(hass) is coord_a


class TestGetDefaultCoordinatorContext:
    """Issue #885: optional `context` enriches the WARNING/throttle token so a
    future occurrence can be attributed to its caller — without breaking the
    message text any existing caller (context=None) already relies on."""

    def test_context_appears_in_warning_message(self, caplog):
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"):
            zone_registry.get_default_coordinator(hass, context="GET /api/climate_advisor/briefing from 1.2.3.4")

        ambiguous_records = [rec for rec in caplog.records if "Multiple Climate Advisor zones" in rec.message]
        assert len(ambiguous_records) == 1
        assert "GET /api/climate_advisor/briefing from 1.2.3.4" in ambiguous_records[0].message

    def test_omitted_context_keeps_original_message_text(self, caplog):
        """No context passed (context=None, the default) — message text unchanged
        from before Issue #885, so no existing substring assertion elsewhere breaks."""
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"):
            zone_registry.get_default_coordinator(hass)

        ambiguous_records = [rec for rec in caplog.records if "Multiple Climate Advisor zones" in rec.message]
        assert len(ambiguous_records) == 1
        assert "Multiple Climate Advisor zones are loaded and this request did not specify a zone" in (
            ambiguous_records[0].message
        )

    def test_different_context_same_entry_logs_again(self, caplog):
        """A different caller (context) hitting the SAME resolved fallback zone is new
        information for observability purposes and must still log — this is the whole
        point of Issue #885 (the live incident could not be attributed to a caller)."""
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"):
            zone_registry.get_default_coordinator(hass, context="GET /a from 1.1.1.1")
            zone_registry.get_default_coordinator(hass, context="GET /b from 2.2.2.2")

        ambiguous_records = [rec for rec in caplog.records if "Multiple Climate Advisor zones" in rec.message]
        assert len(ambiguous_records) == 2

    def test_same_context_repeated_still_throttled_once(self, caplog):
        """Same caller/context hitting the same outcome repeatedly is still throttled to
        one log line — Issue #885 must not regress the original anti-flood fix."""
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"):
            for _ in range(10):
                zone_registry.get_default_coordinator(hass, context="GET /briefing from 1.1.1.1")

        ambiguous_records = [rec for rec in caplog.records if "Multiple Climate Advisor zones" in rec.message]
        assert len(ambiguous_records) == 1


class TestResolveZone:
    """Issue #885: resolve_zone() is the single decision point api.py's
    _get_coordinator() now uses for all 21 REST views instead of each one
    guessing independently."""

    def test_explicit_entry_id_resolves_that_zone_not_refused(self):
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass({"entry_a": coord_a, "entry_b": coord_b})
        coordinator, refused = zone_registry.resolve_zone(hass, "entry_b", context="GET /x from 1.1.1.1")
        assert coordinator is coord_b
        assert refused is False

    def test_single_zone_no_entry_id_not_refused_even_with_allow_guess_false(self):
        """The single-zone convenience path is safe regardless of allow_guess —
        there's nothing ambiguous about it, so a POST on a single-zone install
        must behave exactly as it always has."""
        coord_a = MagicMock()
        hass = _make_hass({"entry_a": coord_a})
        coordinator, refused = zone_registry.resolve_zone(
            hass, None, context="POST /toggle_automation from 1.1.1.1", allow_guess=False
        )
        assert coordinator is coord_a
        assert refused is False

    def test_ambiguous_no_entry_id_allow_guess_true_guesses(self):
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        coordinator, refused = zone_registry.resolve_zone(
            hass, None, context="GET /briefing from 1.1.1.1", allow_guess=True
        )
        assert coordinator is coord_a
        assert refused is False

    def test_ambiguous_no_entry_id_allow_guess_false_refuses(self):
        """The core safety property: a POST-style caller must get None + refused=True,
        never a guessed coordinator — this is what stops a wrong-zone actuation."""
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        coordinator, refused = zone_registry.resolve_zone(
            hass, None, context="POST /toggle_automation from 1.1.1.1", allow_guess=False
        )
        assert coordinator is None
        assert refused is True

    def test_refuse_path_logs_warning_with_context(self, caplog):
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"):
            zone_registry.resolve_zone(hass, None, context="POST /toggle_automation from 9.9.9.9", allow_guess=False)
        records = [rec for rec in caplog.records if "requires an explicit zone" in rec.message]
        assert len(records) == 1
        assert "POST /toggle_automation from 9.9.9.9" in records[0].message

    def test_refuse_path_raises_ambiguous_repairs_issue(self):
        from unittest.mock import patch

        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        with patch("custom_components.climate_advisor.zone_registry.ir.async_create_issue") as mock_create:
            zone_registry.resolve_zone(hass, None, context="POST /toggle_automation from 1.1.1.1", allow_guess=False)
        calls = [c for c in mock_create.call_args_list if "zone_resolution_ambiguous" in c.args]
        assert len(calls) == 1

    def test_refuse_path_repeated_same_context_throttled_once(self, caplog):
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_a", "entry_b"],
        )
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"):
            for _ in range(5):
                zone_registry.resolve_zone(
                    hass, None, context="POST /toggle_automation from 1.1.1.1", allow_guess=False
                )
        records = [rec for rec in caplog.records if "requires an explicit zone" in rec.message]
        assert len(records) == 1

    def test_unknown_entry_id_returns_none_not_refused(self):
        """A bad/stale entry_id is a 'not found' case, not an ambiguity case — must not
        silently fall back to guessing (or refusing) a DIFFERENT zone than the one named."""
        coord_a = MagicMock()
        hass = _make_hass({"entry_a": coord_a})
        coordinator, refused = zone_registry.resolve_zone(
            hass, "entry_does_not_exist", context="POST /x from 1.1.1.1", allow_guess=False
        )
        assert coordinator is None


class TestResolveZoneDisplayName:
    """Issue #976: the AI Investigator report was rendering the raw entry_id
    (coordinator.zone_label) instead of the zone's human-readable entry.title,
    e.g. showing '(zone=01KM12CQSGFV91EPEJXSHZ5Y1K)' instead of '(zone=Zone 1)'."""

    def test_resolves_to_entry_title_when_entry_found(self):
        hass = MagicMock()
        entry = MagicMock()
        entry.title = "Bedroom"
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)
        assert zone_registry.resolve_zone_display_name(hass, "entry_a") == "Bedroom"
        hass.config_entries.async_get_entry.assert_called_once_with("entry_a")

    def test_falls_back_to_entry_id_when_entry_not_found(self):
        hass = MagicMock()
        hass.config_entries.async_get_entry = MagicMock(return_value=None)
        assert zone_registry.resolve_zone_display_name(hass, "entry_removed") == "entry_removed"

    def test_falls_back_to_entry_id_when_title_is_empty(self):
        hass = MagicMock()
        entry = MagicMock()
        entry.title = ""
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)
        assert zone_registry.resolve_zone_display_name(hass, "entry_a") == "entry_a"

    def test_falls_back_to_entry_id_when_hass_has_no_config_entries(self):
        """Lightweight test/harness hass objects (e.g. SimpleNamespace(data={}))
        don't have a config_entries attribute at all — must not raise."""
        hass = SimpleNamespace(data={})
        assert zone_registry.resolve_zone_display_name(hass, "entry_a") == "entry_a"

    def test_returns_falsy_entry_id_unchanged(self):
        hass = MagicMock()
        assert zone_registry.resolve_zone_display_name(hass, None) is None
        assert zone_registry.resolve_zone_display_name(hass, "") == ""


class TestAmbiguousCardSaysWhatAndWhy:
    """Issue #1418: the Repairs card and WARNING name the request, the outcome and the fix (``?zone=``)."""

    def _hass(self):
        coord_a, coord_b = MagicMock(), MagicMock()
        hass = _make_hass({"entry_a": coord_a, "entry_b": coord_b}, ordered_entry_ids=["entry_a", "entry_b"])
        for entry, title in zip(hass.config_entries.async_entries.return_value, ("Zone 1", "Upstairs"), strict=True):
            entry.title = title
        return hass, coord_a

    def test_guess_names_request_and_zone_used(self, caplog):
        from unittest.mock import patch

        hass, coord_a = self._hass()
        with (
            patch("custom_components.climate_advisor.zone_registry.ir.async_create_issue") as mock_create,
            caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"),
        ):
            got, refused = zone_registry.resolve_zone(
                hass, None, context="GET /api/climate_advisor/chart_data from 1.2.3.4"
            )
        assert (got, refused) == (coord_a, False)
        placeholders = mock_create.call_args.kwargs["translation_placeholders"]
        assert placeholders["request"] == "GET /api/climate_advisor/chart_data from 1.2.3.4"
        assert placeholders["outcome"] == 'answered with the data of zone "Zone 1"'
        message = next(r.message for r in caplog.records if "Multiple Climate Advisor zones" in r.message)
        assert "'Zone 1'" in message and "?zone=<entry_id>" in message

    def test_refusal_says_nothing_changed(self):
        from unittest.mock import patch

        hass, _ = self._hass()
        with patch("custom_components.climate_advisor.zone_registry.ir.async_create_issue") as mock_create:
            zone_registry.resolve_zone(hass, None, context="POST /x from 1.1.1.1", allow_guess=False)
        assert mock_create.call_args.kwargs["translation_placeholders"] == {
            "request": "POST /x from 1.1.1.1",
            "outcome": "refused, and nothing was changed",
        }

    def test_card_text_uses_both_placeholders_and_says_how_to_fix(self):
        import json
        from pathlib import Path

        root = Path(zone_registry.__file__).parent
        for name in ("strings.json", "translations/en.json"):
            issue = json.loads((root / name).read_text(encoding="utf-8"))["issues"]["zone_resolution_ambiguous"]
            text = issue["description"]
            assert "{request}" in text and "{outcome}" in text and "?zone=ENTRY_ID" in text, name
            # hassfest rejects anything that reads as HTML in a translation string (CI "validate", #1427)
            assert "<" not in text and ">" not in text, name
            assert "/api/climate_advisor/status" in text, name


class TestOwnershipAndTitleEdges:
    """No entity named means nothing to own; an unknown zone's title falls back to its id (Issues #1144, #1646)."""

    def test_no_fan_entity_is_never_owned(self) -> None:
        hass = _make_hass({"a": MagicMock()})
        assert zone_registry.fan_entity_owned_by_other_zone(hass, None) is False
        assert zone_registry.fan_entity_owned_by_other_zone(hass, "") is False

    def test_no_thermostat_is_never_owned(self) -> None:
        hass = _make_hass({"a": MagicMock()})
        assert zone_registry.thermostat_owned_by_other_zone(hass, None, None) is False
        assert zone_registry.thermostat_owned_by_other_zone(hass, "", "a") is False

    def test_title_of_an_unknown_zone_is_its_entry_id(self) -> None:
        hass = _make_hass({"a": MagicMock()})
        assert zone_registry._zone_title(hass, "missing") == "missing"
