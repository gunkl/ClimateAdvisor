"""Deleting a zone in real Home Assistant removes its files and its Repairs cards; a reload keeps them.

Occupant view: the person deletes a zone from Settings > Devices & services. Its state, learning, chart log and
forecast archive files must be gone from the config dir, and no Repairs card may still offer to "fix" the deleted
zone. Saving settings (a reload) must never delete a zone's data. HA calls ``async_remove_entry`` only on
deletion, after ``async_unload_entry``; this asks real HA that both halves hold.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.climate_advisor.const import DOMAIN
from custom_components.climate_advisor.storage_paths import ENTRY_SCOPED_FILES, resolve_entry_scoped_path
from tests_ha.test_unload_cleanup import _setup_zone


def _zone_files(config_dir: Path, entry_id: str) -> list[Path]:
    return [resolve_entry_scoped_path(config_dir, base, entry_id) for base in ENTRY_SCOPED_FILES]


def _seed_files(paths: list[Path]) -> None:
    for p in paths:
        if not p.exists():
            p.write_text("{}", encoding="utf-8")


def _raise_zone_issues(hass: HomeAssistant, entry_id: str) -> list[str]:
    ids = [f"weather_entity_not_found_{entry_id}", f"reload_needed_{entry_id}"]
    for issue_id in ids:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="reload_needed",
            data={"entry_id": entry_id},
        )
    return ids


async def test_deleting_a_zone_removes_its_files_and_repairs(hass: HomeAssistant, tmp_path) -> None:
    entry = await _setup_zone(hass, tmp_path)
    files = _zone_files(tmp_path, entry.entry_id)
    _seed_files(files)  # the zone may not have written every file in a short run
    issue_ids = _raise_zone_issues(hass, entry.entry_id)
    other_zone = _zone_files(tmp_path, "someOtherZone")
    _seed_files(other_zone)
    legacy = [tmp_path / base for base in ENTRY_SCOPED_FILES]
    _seed_files(legacy)

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    left = [p.name for p in files if p.exists()]
    assert not left, f"files of the deleted zone left on disk: {left}"
    registry = ir.async_get(hass)
    assert all(registry.async_get_issue(DOMAIN, i) is None for i in issue_ids)
    assert all(p.exists() for p in other_zone), "another zone's files were deleted"
    assert all(p.exists() for p in legacy), "a legacy unscoped file was deleted"


async def test_reloading_a_zone_keeps_its_files(hass: HomeAssistant, tmp_path) -> None:
    entry = await _setup_zone(hass, tmp_path)
    files = _zone_files(tmp_path, entry.entry_id)
    _seed_files(files)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert all(p.exists() for p in files), "a reload deleted zone data"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert all(p.exists() for p in files), "an unload deleted zone data"
