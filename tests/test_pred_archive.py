"""Tests for Issue #139 — first-write-wins prediction archive.

Behavioral contract: chart_log[T].pred_indoor should reflect the ODE prediction
made ~4 hours in advance for time T, not the ODE made at time T (which re-seeds
from the actual indoor temp and converges to actual).

Five tests in TestPredArchiveContract:
  1. test_first_write_wins — setdefault never overwrites the first entry.
  2. test_chart_log_uses_archive_not_current_ode — archive wins over current ODE[0].
  3. test_warmup_falls_back_to_current_ode — empty archive → ODE[0] fallback.
  4. test_archive_expiry_removes_old_entries — entries >7 days old are purged.
  5. test_archive_does_not_exceed_horizon — only entries ≤ now+4h are written.
"""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta

# ── HA module stubs (must happen before importing climate_advisor) ──────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import PRED_ARCHIVE_HORIZON_HOURS  # noqa: E402


def _get_coordinator_class():
    """Return ClimateAdvisorCoordinator, fresh each call (avoids stale __globals__)."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


class TestPredArchiveContract:
    """Verify the first-write-wins prediction archive behavioral contract."""

    # ------------------------------------------------------------------
    # 1. setdefault semantics: first write must survive subsequent writes
    # ------------------------------------------------------------------

    def test_first_write_wins(self):
        """Archive entry written first must not be overwritten by later ticks.

        Mirrors the coordinator's setdefault call:
          self._pred_archive.setdefault(self._pred_archive_key(_ae_dt), _ae["temp"])

        A second call with the same key and a different value must leave the
        original value intact.
        """
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._pred_archive = {}

        slot_dt = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)
        key = coord._pred_archive_key(slot_dt)

        # First write — simulates ODE tick at T-4h predicting 76.0°F for this slot
        coord._pred_archive.setdefault(key, 76.0)
        assert coord._pred_archive[key] == 76.0, "First write must be 76.0"

        # Second write — simulates ODE tick at T-2h predicting 78.0°F for the same slot
        coord._pred_archive.setdefault(key, 78.0)
        assert coord._pred_archive[key] == 76.0, (
            f"First-write-wins violated: expected 76.0, got {coord._pred_archive[key]!r}"
        )

    # ------------------------------------------------------------------
    # 2. Archive wins over current ODE during normal operation
    # ------------------------------------------------------------------

    def test_chart_log_uses_archive_not_current_ode(self):
        """When archive has an entry for now_dt, it wins over current ODE[0].

        Simulates the chart_log pred_indoor selection block:
          _archived_pred = self._lookup_pred_archive(_now_dt)   # → 76.0
          if _archived_pred is not None:
              _pred_indoor_val = _archived_pred                  # ← this path
          elif self._last_predicted_indoor:
              _pred_indoor_val = ...  # warmup fallback (not taken)

        The archive value (76.0) was written by an ODE 4h ago; the current ODE[0]
        (74.1) was re-seeded from actual and would collapse to actual if used.
        """
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._pred_archive = {}

        now_dt = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)

        # Pre-populate archive: the 4h-old ODE predicted 76.0°F for this slot
        coord._pred_archive[coord._pred_archive_key(now_dt)] = 76.0

        # Current ODE[0] re-seeds from actual and produces a different value
        coord._last_predicted_indoor = [{"ts": now_dt.isoformat(), "temp": 74.1}]

        # Replicate selection logic from coordinator.py
        _archived_pred = coord._lookup_pred_archive(now_dt)
        _pred_indoor_val: float | None = None
        if _archived_pred is not None:
            _pred_indoor_val = _archived_pred
        elif coord._last_predicted_indoor:
            _pred_indoor_val = coord._last_predicted_indoor[0].get("temp")

        assert _pred_indoor_val == 76.0, (
            f"Archive must win: expected 76.0, got {_pred_indoor_val!r} "
            f"(archive={_archived_pred!r}, ode[0]={coord._last_predicted_indoor[0]['temp']!r})"
        )
        # The two values differ — confirming the archive produces non-trivial divergence
        assert _pred_indoor_val != coord._last_predicted_indoor[0]["temp"], (
            "Archive value must differ from ODE[0] to demonstrate meaningful divergence"
        )

    # ------------------------------------------------------------------
    # 3. Warmup: empty archive falls back to current ODE
    # ------------------------------------------------------------------

    def test_warmup_falls_back_to_current_ode(self):
        """During warmup (first 4h after restart), archive is empty; use ODE[0].

        The warmup path exercises:
          _archived_pred = self._lookup_pred_archive(_now_dt)   # → None (empty archive)
          elif self._last_predicted_indoor:
              _pred_indoor_val = self._last_predicted_indoor[0].get("temp")  # ← this path
        """
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._pred_archive = {}  # empty — simulates fresh restart

        now_dt = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)
        coord._last_predicted_indoor = [{"ts": now_dt.isoformat(), "temp": 71.5}]

        # Replicate selection logic
        _archived_pred = coord._lookup_pred_archive(now_dt)
        _pred_indoor_val: float | None = None
        if _archived_pred is not None:
            _pred_indoor_val = _archived_pred
        elif coord._last_predicted_indoor:
            _pred_indoor_val = coord._last_predicted_indoor[0].get("temp")

        assert _archived_pred is None, "Archive must be empty during warmup"
        assert _pred_indoor_val == 71.5, f"Warmup fallback must return ODE[0]=71.5; got {_pred_indoor_val!r}"

    # ------------------------------------------------------------------
    # 4. Expiry: entries older than 7 days are removed
    # ------------------------------------------------------------------

    def test_archive_expiry_removes_old_entries(self):
        """Expiry pass removes entries with keys older than 7 days, keeps recent ones.

        Mirrors the expiry block from _async_update_data:
          _archive_expire_cutoff = int((dt_util.now() - timedelta(days=7)).timestamp())
          self._pred_archive = {k: v for k, v in self._pred_archive.items()
                                if k >= _archive_expire_cutoff}
        """
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)

        now = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)

        # Build an old key (8 days ago) and a recent key (today)
        old_dt = now - timedelta(days=8)
        recent_dt = now - timedelta(hours=1)

        old_key = coord._pred_archive_key(old_dt)
        recent_key = coord._pred_archive_key(recent_dt)

        coord._pred_archive = {
            old_key: 65.0,
            recent_key: 70.0,
        }

        # Run expiry logic
        cutoff = int((now - timedelta(days=7)).timestamp())
        coord._pred_archive = {k: v for k, v in coord._pred_archive.items() if k >= cutoff}

        assert old_key not in coord._pred_archive, f"Old entry (8 days ago, key={old_key}) must be purged after expiry"
        assert recent_key in coord._pred_archive, f"Recent entry (1h ago, key={recent_key}) must survive expiry"
        assert coord._pred_archive[recent_key] == 70.0, (
            f"Recent entry value must be preserved: expected 70.0, got {coord._pred_archive[recent_key]!r}"
        )

    # ------------------------------------------------------------------
    # 5. Horizon guard: only entries within PRED_ARCHIVE_HORIZON_HOURS are written
    # ------------------------------------------------------------------

    def test_archive_does_not_exceed_horizon(self):
        """Archive population skips ODE entries beyond PRED_ARCHIVE_HORIZON_HOURS.

        Mirrors the archive population loop in _async_update_data:
          _archive_cutoff = dt_util.now() + timedelta(hours=PRED_ARCHIVE_HORIZON_HOURS)
          for _ae in self._last_predicted_indoor:
              _ae_dt = datetime.fromisoformat(_ae["ts"])
              if _ae_dt > _archive_cutoff:
                  break
              self._pred_archive.setdefault(self._pred_archive_key(_ae_dt), _ae["temp"])

        With PRED_ARCHIVE_HORIZON_HOURS=4, entries at hours 5, 6, ... must not appear.
        """
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._pred_archive = {}

        now = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)

        # Build a 24-entry hourly ODE curve (1h intervals starting from now+30min
        # to mimic real ODE output which starts slightly ahead of now)
        forecast: list[dict] = []
        for h in range(24):
            entry_dt = now + timedelta(hours=h)
            forecast.append({"ts": entry_dt.isoformat(), "temp": 68.0 + h * 0.5})

        # Run archive population (replicated from coordinator)
        archive_cutoff = now + timedelta(hours=PRED_ARCHIVE_HORIZON_HOURS)
        for ae in forecast:
            try:
                ae_dt = datetime.fromisoformat(ae["ts"])
            except (ValueError, KeyError):
                continue
            if ae_dt > archive_cutoff:
                break
            coord._pred_archive.setdefault(coord._pred_archive_key(ae_dt), ae["temp"])

        # Verify: all written entries are within horizon
        for key in coord._pred_archive:
            # Convert key (Unix epoch) back to datetime for assertion
            key_dt = datetime.fromtimestamp(key, tz=UTC)
            assert key_dt <= archive_cutoff, (
                f"Archive entry at {key_dt.isoformat()} exceeds horizon "
                f"{archive_cutoff.isoformat()} (PRED_ARCHIVE_HORIZON_HOURS={PRED_ARCHIVE_HORIZON_HOURS})"
            )

        # Verify: hour 5+ entries are absent (their keys should not appear)
        hour5_dt = now + timedelta(hours=5)
        hour5_key = coord._pred_archive_key(hour5_dt)
        assert hour5_key not in coord._pred_archive, (
            f"Entry at hour+5 ({hour5_dt.isoformat()}) must not be in archive "
            f"(horizon is {PRED_ARCHIVE_HORIZON_HOURS}h)"
        )

        # Verify: at least the within-horizon entries are present (sanity check)
        assert len(coord._pred_archive) > 0, "Archive must contain at least some entries within horizon"
        assert len(coord._pred_archive) <= PRED_ARCHIVE_HORIZON_HOURS + 2, (
            f"Archive contains {len(coord._pred_archive)} entries — "
            f"expected at most {PRED_ARCHIVE_HORIZON_HOURS + 2} for a {PRED_ARCHIVE_HORIZON_HOURS}h horizon"
        )
