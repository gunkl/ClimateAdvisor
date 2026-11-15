"""Issue #1341 Stage 1B: the dashboard i18n wiring on the Python side.

Occupant: after an update that fixes a translation, the panel iframe must not keep showing the cached old text, and
the dashboard must be told the house's language even when the browser says something else.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor import _panel_asset_bytes, i18n
from custom_components.climate_advisor.api import ClimateAdvisorStatusView
from tools.sim_harness.build_coordinator import build_headless_multi_zone

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "custom_components" / "climate_advisor" / "frontend"


def _frontend_copy(tmp_path: Path) -> Path:
    fe = tmp_path / "frontend"
    (fe / "i18n" / "common").mkdir(parents=True)
    for name in ("entry.html", "index.html", "new_dashboard.html", "i18n.js", "intl-messageformat.iife.js"):
        (fe / name).write_text(f"// {name}", encoding="utf-8")
    (fe / "i18n" / "index.json").write_text('{"languages":["en"],"areas":["common"]}', encoding="utf-8")
    (fe / "i18n" / "common" / "en.json").write_text('{"common.x":"X"}', encoding="utf-8")
    (fe / "i18n" / "common" / "de.json").write_text('{"common.x":"Ex"}', encoding="utf-8")
    return fe


def test_panel_hash_input_changes_when_a_catalog_file_changes(tmp_path):
    fe = _frontend_copy(tmp_path)
    before = _panel_asset_bytes(fe)
    assert _panel_asset_bytes(fe) == before  # stable across reads (sorted walk)
    (fe / "i18n" / "common" / "de.json").write_text('{"common.x":"Ix"}', encoding="utf-8")
    assert _panel_asset_bytes(fe) != before


def test_panel_hash_input_changes_when_a_catalog_file_is_added_or_the_runtime_changes(tmp_path):
    fe = _frontend_copy(tmp_path)
    before = _panel_asset_bytes(fe)
    (fe / "i18n" / "common" / "fr.json").write_text("{}", encoding="utf-8")
    added = _panel_asset_bytes(fe)
    assert added != before
    (fe / "i18n.js").write_text("// changed", encoding="utf-8")
    assert _panel_asset_bytes(fe) != added


def test_panel_hash_covers_the_real_runtime_files():
    data = _panel_asset_bytes(FE)
    assert (FE / "i18n.js").read_bytes() in data
    assert (FE / "intl-messageformat.iife.js").read_bytes() in data


def test_status_payload_carries_the_backend_language():
    zones, fake_hass, _ = build_headless_multi_zone(zone_count=1)
    assert len(zones) == 1
    fake_hass.config.language = "pt-BR"
    request = MagicMock()
    request.app = {"hass": fake_hass}
    request.query = {}
    body = asyncio.run(ClimateAdvisorStatusView().get(request)).json_data
    assert "language" in body
    # real resolution is pinned in tests/test_i18n*.py by the engine stage; here: present, a non-empty code
    assert isinstance(body["language"], str) and body["language"]


@pytest.mark.parametrize("breakage", ["malformed_json", "load_raises"])
def test_a_broken_catalog_does_not_stop_setup(breakage, tmp_path, monkeypatch, caplog):
    """Issue #1341 S1: occupant keeps a working thermostat even if a translation file is broken.

    ``async_setup_entry`` logs the reason and carries on; text then falls back to English, then to the key.
    """
    i18n.reset_catalogs()
    i18n.set_strict(False)  # a broken catalog is the non-strict production path; the session fixture re-arms strict
    if breakage == "malformed_json":
        (tmp_path / "i18n").mkdir()
        (tmp_path / "i18n" / "index.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(i18n, "default_i18n_dir", lambda: tmp_path / "i18n")
        reason = "Expecting property name"
    else:

        def boom(*_a, **_k):
            raise i18n.I18nError("catalog is bad")

        monkeypatch.setattr(i18n, "load_catalogs", boom)
        reason = "catalog is bad"
    with caplog.at_level(logging.ERROR, logger="custom_components.climate_advisor"):
        zones, _hass, _scheduler = build_headless_multi_zone(zone_count=1)  # runs the real async_setup_entry
    assert zones["zone_0"]["coordinator"] is not None
    errors = [r.getMessage() for r in caplog.records if "translation catalogs" in r.getMessage()]
    assert errors and reason in errors[0]
    assert not i18n.catalogs_loaded()
    # rendering falls back (non-strict): the key itself, never an exception
    assert i18n.render(i18n.Msg("common.mode.heat", x=1), "de") == "common.mode.heat"


def test_setup_loads_the_catalogs_through_the_executor_and_a_failed_attempt_is_not_retried(monkeypatch):
    """Issue #1341 S2/S3: ``async_setup_entry`` reads the catalogs in the executor with the one lazy loader. A failed
    attempt is recorded, so no later render path re-reads files (in the event loop) and none of them raises."""
    from tools.sim_harness.fake_hass import FakeHass

    seen: list = []
    original = FakeHass.async_add_executor_job

    def spy(self, fn, *args):
        seen.append(fn)
        return original(self, fn, *args)

    monkeypatch.setattr(FakeHass, "async_add_executor_job", spy)
    zones, _hass, _scheduler = build_headless_multi_zone(zone_count=1)
    assert zones["zone_0"]["coordinator"] is not None
    assert i18n.ensure_catalogs in seen
    assert i18n.catalogs_loaded()

    # a failed first attempt: non-strict, nothing raises, and the failure is never retried
    i18n.reset_catalogs()
    i18n.set_strict(False)  # this test only; the session fixture re-arms strict
    calls: list = []

    def boom(*_a, **_k):
        calls.append(1)
        raise i18n.I18nError("catalog is bad")

    monkeypatch.setattr(i18n, "load_catalogs", boom)
    try:
        i18n.ensure_catalogs()
        i18n.ensure_catalogs()
        assert calls == [1]
        assert not i18n.catalogs_loaded()
        from custom_components.climate_advisor.operation_timeline import render_msg
        from custom_components.climate_advisor.record_text import record_en

        assert render_msg(i18n.Msg("common.mode.heat"), "de") == "common.mode.heat"  # key fallback, no raise
        assert record_en(i18n.Msg("common.mode.heat")) == "common.mode.heat"
        assert calls == [1]  # render paths never reloaded
    finally:
        i18n.reset_catalogs()
        monkeypatch.undo()
        i18n.load_catalogs()
