"""Guard core (tools/i18n_checks.py) and the tools/i18n.py CLI (Issue #1341)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tools import i18n as cli

checks = cli.checks  # the module the CLI itself uses, so monkeypatching reaches both
REPO = Path(__file__).resolve().parent.parent
FRONTEND_I18N = REPO / "custom_components" / "climate_advisor" / "frontend" / "i18n"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A throwaway copy of the catalogs, locks and translations that the checks and CLI read."""
    i18n_dir = tmp_path / "i18n_catalogs"
    locks = tmp_path / "locks"
    trans = tmp_path / "translations"
    shutil.copytree(FRONTEND_I18N, i18n_dir)
    shutil.copytree(REPO / "i18n" / "locks", locks)
    shutil.copytree(REPO / "custom_components" / "climate_advisor" / "translations", trans)
    monkeypatch.setattr(checks, "I18N_DIR", i18n_dir)
    monkeypatch.setattr(checks, "LOCKS_DIR", locks)
    monkeypatch.setattr(checks, "TRANSLATIONS_DIR", trans)
    return i18n_dir, locks, trans


def _edit(path: Path, **changes):
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_committed_repository_passes_every_guard():
    results = checks.run_all()
    assert results == {"parity": [], "ha parity": [], "stale": []}


def test_cli_check_passes_on_the_repo(capsys):
    assert cli.main(["check"]) == 0
    assert "[FAIL]" not in capsys.readouterr().out


def test_lists_in_index_match_the_engine():
    eng = checks.engine()
    index = json.loads((FRONTEND_I18N / "index.json").read_text(encoding="utf-8"))
    assert tuple(index["languages"]) == eng.LANGUAGES and tuple(index["areas"]) == eng.AREAS


def test_every_language_has_every_area_file_and_the_common_keys():
    data, errors = checks.load_catalogs()
    assert errors == []
    keys = set(data["common"]["en"])
    assert {"common.mode.heat", "common.mode.fan_only", "common.unknown", "common.duration.min"} <= keys
    for area, langs in data.items():
        for lang, catalog in langs.items():
            assert set(catalog) == set(data[area]["en"]), (area, lang)


# ------------------------------------------------------------------------------------------ parity failures
def _fr(tree):
    return tree[0] / "common" / "fr.json"


def test_parity_flags_missing_and_extra_keys(tree):
    _edit(_fr(tree), **{"common.unknown": None, "common.extra": "x"})
    errors = checks.check_parity()
    assert any("missing key common.unknown" in e for e in errors)
    assert any("extra key common.extra" in e for e in errors)


def test_parity_flags_key_without_area_prefix(tree):
    _edit(_fr(tree), **{"other.key": "x"})
    assert any("must start with 'common.'" in e for e in checks.check_parity())


def test_parity_flags_argument_name_change(tree):
    _edit(tree[0] / "common" / "en.json", **{"common.t": "Hello {name}"})
    _edit(_fr(tree), **{"common.t": "Bonjour {nom}"})
    assert any("arguments" in e and "common.t" in e for e in checks.check_parity())


def test_parity_flags_select_variant_mismatch_and_accepts_equal(tree):
    _edit(tree[0] / "common" / "en.json", **{"common.s": "{g, select, f {a} m {b} other {c}}"})
    _edit(_fr(tree), **{"common.s": "{g, select, f {a} other {c}}"})
    assert any("variants" in e and "common.s" in e for e in checks.check_parity())
    _edit(_fr(tree), **{"common.s": "{g, select, f {x} m {y} other {z}}"})
    assert not [e for e in checks.check_parity() if "common/fr" in e and "common.s" in e]


def test_parity_flags_plural_category_outside_the_language(tree):
    _edit(tree[0] / "common" / "en.json", **{"common.p": "{n, plural, one {# a} other {# b}}"})
    _edit(_fr(tree), **{"common.p": "{n, plural, one {# a} few {# c} other {# b}}"})
    errors = checks.check_parity()
    assert any("not CLDR cardinal categories of fr" in e for e in errors)
    _edit(_fr(tree), **{"common.p": "{n, plural, one {# a} many {# c} other {# b}}"})  # fr has many
    assert not [e for e in checks.check_parity() if "common/fr" in e and "common.p" in e]


def test_parity_requires_other_and_valid_syntax(tree):
    _edit(_fr(tree), **{"common.unknown": "{n, plural, one {x}}"})
    assert any("common.unknown" in e and "other" in e for e in checks.check_parity())
    _edit(_fr(tree), **{"common.unknown": "<b>bold</b>"})
    assert any("tags are not supported" in e for e in checks.check_parity())


def test_parity_flags_english_plural_categories_too(tree):
    _edit(tree[0] / "common" / "en.json", **{"common.p": "{n, plural, one {a} few {b} other {c}}"})
    assert any("common/en" in e or "common/en:" in e for e in checks.check_parity())


def test_parity_flags_a_missing_file(tree):
    _fr(tree).unlink()
    assert any("fr.json unreadable" in e for e in checks.check_parity())


# ------------------------------------------------------------------------------------------ HA parity
def _ha(tree, lang, content):
    (tree[2] / f"{lang}.json").write_text(json.dumps(content), encoding="utf-8")


def _only_ha(tree, *langs):
    """Keep only the named HA language files in the throwaway tree (the repo now ships 16)."""
    for path in tree[2].glob("*.json"):
        if path.stem not in langs:
            path.unlink()


def test_ha_parity_passes_for_matching_translation(tree):
    _only_ha(tree, "en", "fr")
    _ha(tree, "fr", {"a": {"b": "Bonjour {name}"}})
    _ha(tree, "en", {"a": {"b": "Hello {name}"}})
    assert checks.check_ha_parity() == []


def test_ha_parity_flags_keys_and_placeholders(tree):
    _only_ha(tree, "en", "fr")
    _ha(tree, "en", {"a": {"b": "Hello {name}", "c": "x"}})
    _ha(tree, "fr", {"a": {"b": "Bonjour {nom}", "d": "y"}})
    errors = checks.check_ha_parity()
    assert any("missing key a.c" in e for e in errors)
    assert any("extra key a.d" in e for e in errors)
    assert any("a.b" in e and "placeholders" in e for e in errors)


def test_ha_parity_rejects_icu_values(tree):
    _ha(tree, "en", {"a": "{n, plural, one {x} other {y}}"})
    assert any("unsupported placeholder" in e or "Single" in e or "expected" in e for e in checks.check_ha_parity())
    _ha(tree, "en", {"a": "{n, number}"})
    assert checks.check_ha_parity()
    _ha(tree, "en", {"a": "unbalanced {name"})
    assert checks.check_ha_parity()


# ------------------------------------------------------------------------------------------ staleness
def test_changed_english_makes_every_translation_stale_until_relocked(tree):
    assert checks.check_stale() == []
    _edit(tree[0] / "common" / "en.json", **{"common.unknown": "Not known"})
    errors = checks.check_stale()
    others = len(checks.engine().LANGUAGES) - 1  # every translation of the changed key goes stale
    assert len(errors) == others and all("common.unknown" in e and "English changed" in e for e in errors)
    assert cli.write_lock("common", "fr") > 0
    assert len(checks.check_stale()) == others - 1
    assert cli.write_lock("common", None) > 0
    assert checks.check_stale() == []


def test_new_translation_without_lock_is_stale_and_lock_records_it(tree):
    _edit(tree[0] / "common" / "en.json", **{"common.new": "New"})
    _edit(_fr(tree), **{"common.new": "Nouveau"})
    errors = checks.check_stale()
    assert (
        errors == [e for e in errors if "common/fr" in e and "common.new" in e and "not locked" in e]
        and len(errors) == 1
    )
    cli.write_lock("common", "fr")
    assert checks.check_stale() == []
    lock = json.loads((tree[1] / "common.json").read_text(encoding="utf-8"))
    assert lock["fr"]["common.new"] == checks.en_hash("New")


def test_missing_is_not_stale_and_lock_ignores_absent_keys(tree):
    _edit(tree[0] / "common" / "en.json", **{"common.only_en": "Only English"})
    assert checks.check_stale() == []
    assert ("common", "fr", "common.only_en") in checks.missing_keys(checks.catalogs_by_area())
    cli.write_lock("common", "fr")
    lock = json.loads((tree[1] / "common.json").read_text(encoding="utf-8"))
    assert "common.only_en" not in lock["fr"]


def test_ha_staleness_uses_the_ha_lock(tree):
    _ha(tree, "en", {"a": "Hello"})
    _ha(tree, "fr", {"a": "Bonjour"})
    assert any("ha/fr" in e and "not locked" in e for e in checks.check_stale())
    cli.write_lock("ha", "fr")
    assert checks.check_stale() == []
    _ha(tree, "en", {"a": "Hello there"})
    assert any("ha/fr: a is stale" in e for e in checks.check_stale())


def test_en_hash_is_twelve_hex_of_sha1():
    assert checks.en_hash("abc") == "a9993e364706"


# ------------------------------------------------------------------------------------------ CLI
def test_cli_missing_stale_lock_and_check_exit_codes(tree, capsys):
    _edit(tree[0] / "common" / "en.json", **{"common.new": "New"})
    assert cli.main(["missing", "--area", "common", "--lang", "fr"]) == 0
    assert "common/fr: missing common.new" in capsys.readouterr().out
    assert cli.main(["check"]) == 1  # parity: key missing in 15 languages
    out = capsys.readouterr().out
    assert "[FAIL] parity" in out and "[ok] stale" in out
    for lang in checks.engine().LANGUAGES[1:]:
        _edit(tree[0] / "common" / f"{lang}.json", **{"common.new": f"new-{lang}"})
    assert cli.main(["check"]) == 1  # now only stale (not locked)
    assert "[FAIL] stale" in capsys.readouterr().out
    assert cli.main(["stale", "--area", "common"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"{len(checks.engine().LANGUAGES) - 1} stale"
    assert cli.main(["lock", "--area", "common", "--lang", "all"]) == 0
    assert cli.main(["check"]) == 0


def test_cli_rejects_unknown_area_and_language(tree):
    with pytest.raises(SystemExit):
        cli.main(["missing", "--area", "nope"])
    with pytest.raises(SystemExit):
        cli.main(["missing", "--lang", "xx"])
    with pytest.raises(SystemExit):
        cli.main(["lock", "--area", "common"])
