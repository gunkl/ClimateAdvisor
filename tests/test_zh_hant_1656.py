"""Issue #1656: Traditional Chinese (Taiwan), zh-Hant.

Occupant story: someone in Taiwan runs Home Assistant in Traditional Chinese. Before, Climate Advisor showed them
English (zh-TW / zh-HK / zh-Hant mapped to en, because Simplified characters read as wrong). Now it shows a
translation written for Taiwan. If a later change slipped Simplified characters or mainland words into it, it
would read as a careless conversion, so the zh-Hant wording guard fails on them.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor import i18n  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import i18n_checks as checks  # noqa: E402


@pytest.mark.parametrize("code", ["zh-TW", "zh-HK", "zh-MO", "zh-Hant", "zh-Hant-TW", "zh_Hant_HK"])
def test_traditional_codes_resolve_to_zh_hant(code):
    assert i18n.resolve_language(code) == "zh-Hant"


@pytest.mark.parametrize("code", ["zh", "zh-CN", "zh-SG", "zh-Hans", "zh-Hans-TW"])
def test_simplified_codes_still_resolve_to_zh_hans(code):
    assert i18n.resolve_language(code) == "zh-Hans"


def test_time_and_lists_follow_taiwan_usage():
    assert i18n.plural_category(1, "zh-Hant") == "other"
    terms = json.loads(checks.ZH_HANT_TERMS_PATH.read_text(encoding="utf-8"))
    assert len(terms["simplified_chars"]) > 400
    assert {"term": "默認", "taiwan": "預設"} in terms["mainland_only"]


def test_committed_zh_hant_catalogs_pass_the_wording_guard():
    assert checks.check_zh_hant() == []


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    i18n_dir = tmp_path / "i18n"
    shutil.copytree(checks.FRONTEND_DIR / "i18n", i18n_dir)
    tr_dir = tmp_path / "translations"
    shutil.copytree(checks.TRANSLATIONS_DIR, tr_dir)
    return i18n_dir, tr_dir


def test_guard_flags_a_simplified_character_and_a_mainland_term(tmp_path):
    i18n_dir, tr_dir = _tree(tmp_path)
    path = i18n_dir / "common" / "zh-Hant.json"
    cat = json.loads(path.read_text(encoding="utf-8"))
    key = next(iter(cat))
    cat[key] = "请检查默認值"  # 请 and 检 are Simplified; 默認 is mainland for 預設
    path.write_text(json.dumps(cat, ensure_ascii=False), encoding="utf-8")
    errors = checks.check_zh_hant(i18n_dir, tr_dir)
    assert any(key in e and "Simplified" in e for e in errors), errors
    assert any(key in e and "默認" in e and "預設" in e for e in errors), errors


def test_guard_ignores_argument_names_and_checks_the_ha_catalog(tmp_path):
    i18n_dir, tr_dir = _tree(tmp_path)
    path = tr_dir / "zh-Hant.json"
    ha = json.loads(path.read_text(encoding="utf-8"))
    ha["exceptions"]["unknown_zone"]["message"] = "找不到區域 {entry_id}，请確認。"
    path.write_text(json.dumps(ha, ensure_ascii=False), encoding="utf-8")
    errors = checks.check_zh_hant(i18n_dir, tr_dir)
    assert errors == ["ha/zh-Hant: exceptions.unknown_zone.message: Simplified character(s) 请"]


def test_check_runs_the_wording_guard():
    assert "zh-Hant wording" in checks.run_guards()
