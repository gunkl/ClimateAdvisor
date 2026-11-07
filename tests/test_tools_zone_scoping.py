"""Issue #1418: the REST diagnostic tools name their zone instead of silently reporting the first one.

Occupant/owner view: with several zones, ``thermal_health.py`` or ``learning_db.py --entry-id <zone 3>`` used to
print Zone 1's live pipeline (the REST call carried no zone), so a fault in zone 3 was diagnosed from zone 1's data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import _zone_storage as zs  # noqa: E402

ZONES = [{"entry_id": "01AAA", "title": "Zone 1"}, {"entry_id": "01BBB", "title": "Upstairs"}]


def test_with_zone_appends_zone_param():
    assert zs.with_zone("http://h/api/climate_advisor/event_log?hours=2", "01BBB") == (
        "http://h/api/climate_advisor/event_log?hours=2&zone=01BBB"
    )
    assert zs.with_zone("http://h/api/climate_advisor/automation_state", "01BBB").endswith("?zone=01BBB")
    assert zs.with_zone("http://h/x", None) == "http://h/x"


def test_resolve_api_zone_explicit_wins_without_a_status_call():
    def boom(*_a, **_k):
        raise AssertionError("status should not be read when --entry-id is given")

    assert zs.resolve_api_zone("http://h", "t", "01BBB", get_json=boom) == "01BBB"


def test_resolve_api_zone_single_zone_is_unscoped():
    assert zs.resolve_api_zone("http://h", "t", None, get_json=lambda *_: {"indoor_temp": 70}) is None


def test_resolve_api_zone_status_unreadable_is_unscoped():
    assert zs.resolve_api_zone("http://h", "t", None, get_json=lambda *_: None) is None


def test_resolve_api_zone_multi_zone_requires_a_choice():
    seen = []

    def status(url, token):
        seen.append(url)
        return {"zone_selection_required": True, "zones": ZONES, "zone_count": 2}

    with pytest.raises(zs.ZoneSelectionRequired) as exc:
        zs.resolve_api_zone("http://h/", "t", None, get_json=status)
    assert seen == ["http://h/api/climate_advisor/status"]
    assert exc.value.zones == ZONES
    listing = zs.format_zone_choices(exc.value.zones)
    assert "01AAA  Zone 1" in listing and "01BBB  Upstairs" in listing


def test_entry_id_from_remote_path_round_trips():
    for base in (zs.LEARNING_DB_FILE, zs.CHART_LOG_FILE):
        assert zs.entry_id_from_remote_path(base, zs.remote_path(base, "01BBB")) == "01BBB"
        assert zs.entry_id_from_remote_path(base, zs.remote_path(base, None)) is None


def test_learning_db_live_fetch_names_the_zone(monkeypatch):
    import importlib

    learning_db = importlib.import_module("learning_db")
    urls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"thermal_pipeline": {}}'

    def fake_urlopen(req, timeout=10):
        urls.append(req.full_url)
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert learning_db._fetch_live_pipeline("http://h", "t", "01BBB") == {"thermal_pipeline": {}}
    assert urls == ["http://h/api/climate_advisor/automation_state?zone=01BBB"]


def test_thermal_health_multi_zone_without_entry_id_stops_with_the_zone_list(monkeypatch, capsys):
    import importlib

    thermal_health = importlib.import_module("thermal_health")
    monkeypatch.setenv("HA_URL", "http://h")
    monkeypatch.setenv("HA_TOKEN", "t")

    def fake_resolve(url, token, entry_id):
        raise zs.ZoneSelectionRequired(ZONES)

    def no_sensor(*_a, **_k):
        raise AssertionError("must not read a sensor before the zone is known")

    monkeypatch.setattr(thermal_health, "resolve_api_zone", fake_resolve)
    monkeypatch.setattr(thermal_health, "_fetch_sensor", no_sensor)
    with pytest.raises(SystemExit) as exc:
        thermal_health.main([])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "--entry-id" in out and "01BBB  Upstairs" in out


def test_thermal_health_zone_scopes_sensor_and_debug_state(monkeypatch):
    import importlib

    thermal_health = importlib.import_module("thermal_health")
    monkeypatch.setenv("HA_URL", "http://h")
    monkeypatch.setenv("HA_TOKEN", "t")
    calls = {}
    monkeypatch.setattr(thermal_health, "_resolve_sensor_entity", lambda u, t, e: f"sensor.upstairs_{e}")

    def fake_sensor(url, token, entity_id):
        calls["sensor"] = entity_id
        return {"attributes": {"thermal_learning_health": {}}}

    def fake_debug(url, token, entry_id=None):
        calls["debug"] = entry_id
        return None

    monkeypatch.setattr(thermal_health, "_fetch_sensor", fake_sensor)
    monkeypatch.setattr(thermal_health, "_fetch_debug_state", fake_debug)
    monkeypatch.setattr(thermal_health, "_print_report", lambda h: None)
    thermal_health.main(["--entry-id", "01BBB"])
    assert calls == {"sensor": "sensor.upstairs_01BBB", "debug": "01BBB"}


def test_thermal_health_sensor_template_filters_by_zone():
    import importlib

    thermal_health = importlib.import_module("thermal_health")
    tpl = thermal_health._zone_sensor_template("01BBB")
    assert "config_entry_id(e) == '01BBB'" in tpl and "thermal_learning_health" in tpl
