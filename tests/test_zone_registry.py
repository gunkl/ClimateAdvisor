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
from unittest.mock import MagicMock

from custom_components.climate_advisor import zone_registry
from custom_components.climate_advisor.const import DOMAIN


def _make_hass(coordinators: dict[str, MagicMock], ordered_entry_ids: list[str] | None = None) -> MagicMock:
    """Build a fake hass with hass.data[DOMAIN] and a stable async_entries() order.

    ordered_entry_ids, when given, drives hass.config_entries.async_entries()'s
    return order — independent of hass.data[DOMAIN]'s (a plain dict's) own
    iteration order, so tests can prove the fallback uses the CORRECT stable
    source, not whatever a dict happens to do.
    """
    hass = MagicMock()
    hass.data = {DOMAIN: dict(coordinators)}

    if ordered_entry_ids is not None:
        entries = []
        for entry_id in ordered_entry_ids:
            entry = MagicMock()
            entry.entry_id = entry_id
            entries.append(entry)
        hass.config_entries.async_entries = MagicMock(return_value=entries)
    else:
        hass.config_entries.async_entries = MagicMock(return_value=[])

    return hass


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

        entry_b is inserted into hass.data[DOMAIN] first (so plain dict
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

    def test_falls_back_to_dict_order_when_async_entries_empty(self):
        """Defensive path: hass.data non-empty but async_entries() returns nothing."""
        coord_a = MagicMock()
        hass = _make_hass({"entry_a": coord_a, "entry_b": MagicMock()}, ordered_entry_ids=[])
        result = zone_registry.get_default_coordinator(hass)
        assert result is not None

    def test_defensive_empty_config_entries_path_logs_warning(self, caplog):
        """N1: the previously-silent 'async_entries() returned nothing' path now warns."""
        hass = _make_hass({"entry_a": MagicMock(), "entry_b": MagicMock()}, ordered_entry_ids=[])
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"):
            zone_registry.get_default_coordinator(hass)
        assert any("async_entries() returned none" in rec.message for rec in caplog.records)

    def test_defensive_no_matching_loaded_entry_path_logs_warning(self, caplog):
        """N1: the previously-silent 'no ordered entry matched hass.data' path now warns."""
        hass = _make_hass(
            {"entry_a": MagicMock(), "entry_b": MagicMock()},
            ordered_entry_ids=["entry_pending_1", "entry_pending_2"],
        )
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.zone_registry"):
            zone_registry.get_default_coordinator(hass)
        assert any("matched a loaded coordinator" in rec.message for rec in caplog.records)


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
            del hass.data[DOMAIN]["entry_a"]
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

    def test_defensive_empty_config_entries_picks_lowest_entry_id_regardless_of_dict_order(self):
        """Defensive path (async_entries() empty): tie-break must sort by entry_id, not
        dict-insertion order. Same zones inserted in two different orders must both
        resolve to the SAME coordinator (the one whose entry_id sorts first)."""
        coord_a, coord_b, coord_c = MagicMock(), MagicMock(), MagicMock()

        hass_forward = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b, "entry_c": coord_c},
            ordered_entry_ids=[],
        )
        hass_scrambled = _make_hass(
            {"entry_c": coord_c, "entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=[],
        )

        assert zone_registry.get_default_coordinator(hass_forward) is coord_a
        assert zone_registry.get_default_coordinator(hass_scrambled) is coord_a

    def test_defensive_no_matching_loaded_entry_picks_lowest_entry_id_regardless_of_dict_order(self):
        """Defensive path (no ordered entry matches hass.data): same determinism proof
        as above, but via the second fallback branch (ordered entries all unloaded)."""
        coord_a, coord_b, coord_c = MagicMock(), MagicMock(), MagicMock()

        hass_forward = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b, "entry_c": coord_c},
            ordered_entry_ids=["entry_pending_1", "entry_pending_2"],
        )
        hass_scrambled = _make_hass(
            {"entry_c": coord_c, "entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_pending_1", "entry_pending_2"],
        )

        assert zone_registry.get_default_coordinator(hass_forward) is coord_a
        assert zone_registry.get_default_coordinator(hass_scrambled) is coord_a

    def test_skips_config_entries_not_yet_in_hass_data(self):
        """async_entries() may list an entry mid-setup, before hass.data[DOMAIN] has it —
        the fallback must skip past it to the next entry that IS actually loaded."""
        coord_a = MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a},
            ordered_entry_ids=["entry_pending", "entry_a"],
        )
        # len(hass.data[DOMAIN]) is 1 here, so this actually exercises the
        # single-zone convenience path — add a second loaded zone to force
        # the multi-zone branch while still including an unloaded entry.
        coord_b = MagicMock()
        hass = _make_hass(
            {"entry_a": coord_a, "entry_b": coord_b},
            ordered_entry_ids=["entry_pending", "entry_a", "entry_b"],
        )
        assert zone_registry.get_default_coordinator(hass) is coord_a
