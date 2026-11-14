"""Guard core for the Climate Advisor translations (Issue #1341). Pure standard library.

Imported by ``tools/validate.py`` (the pre-commit hook and CI) and by the ``tools/i18n.py`` CLI, so the gate and the
tool cannot disagree. Every ``check_*`` function returns a list of human-readable error strings (empty = pass).

* ``check_parity``    every ``frontend/i18n/<area>/<lang>.json`` has exactly English's key set, the same argument
                      names and select/plural variants, only CLDR categories for its language, and parses under
                      the ICU subset.
* ``check_ha_parity`` every ``translations/<lang>.json`` has English's nested keys and ``{placeholders}``, and every
                      value is a plain ``str.format`` template (no ICU).
* ``check_stale``     a translation is current only while the hash of the English text it was written against
                      (``i18n/locks/<area>.json``, ``ha.json``) equals the English text now.

The ICU parser is the production one: ``custom_components/climate_advisor/i18n.py`` is loaded by file path (it has no
Home Assistant import), so this module also works in a bare interpreter.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import string
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO / "custom_components" / "climate_advisor"
I18N_DIR = COMPONENT_DIR / "frontend" / "i18n"
LOCKS_DIR = REPO / "i18n" / "locks"
TRANSLATIONS_DIR = COMPONENT_DIR / "translations"
HA_AREA = "ha"
HASH_LEN = 12

_ENGINE: Any = None


def engine() -> Any:
    """The production ``i18n`` module, loaded standalone from its file."""
    global _ENGINE  # noqa: PLW0603
    if _ENGINE is None:
        name = "climate_advisor_i18n_standalone"
        spec = importlib.util.spec_from_file_location(name, COMPONENT_DIR / "i18n.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _ENGINE = module
    return _ENGINE


def en_hash(text: str) -> str:
    """First 12 hex chars of the SHA-1 of an English value (the staleness lock)."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:HASH_LEN]  # noqa: S324 - a change fingerprint, not security


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------ catalogs
def load_catalogs(i18n_dir: Path | None = None) -> tuple[dict[str, dict[str, dict[str, str]]], list[str]]:
    """Read ``{area: {lang: {key: message}}}`` directly from disk; also return structural errors."""
    i18n_dir = i18n_dir or I18N_DIR
    eng = engine()
    errors: list[str] = []
    data: dict[str, dict[str, dict[str, str]]] = {}
    try:
        index = _read_json(i18n_dir / "index.json")
    except (OSError, ValueError) as err:
        return data, [f"index.json unreadable: {err}"]
    if tuple(index.get("languages", ())) != eng.LANGUAGES:
        errors.append("index.json languages differ from i18n.LANGUAGES")
    if tuple(index.get("areas", ())) != eng.AREAS:
        errors.append("index.json areas differ from i18n.AREAS")
    for area in eng.AREAS:
        data[area] = {}
        for lang in eng.LANGUAGES:
            path = i18n_dir / area / f"{lang}.json"
            try:
                content = _read_json(path)
            except (OSError, ValueError) as err:
                errors.append(f"{area}/{lang}.json unreadable: {err}")
                content = {}
            if not isinstance(content, dict):
                errors.append(f"{area}/{lang}.json is not a JSON object")
                content = {}
            clean: dict[str, str] = {}
            for key, value in content.items():
                if not key.startswith(area + "."):
                    errors.append(f"{area}/{lang}.json: key {key!r} must start with '{area}.'")
                elif not isinstance(value, str):
                    errors.append(f"{area}/{lang}.json: value of {key!r} is not a string")
                else:
                    clean[key] = value
            data[area][lang] = clean
    return data, errors


# ------------------------------------------------------------------------------------------------ parity
def message_signature(ast: tuple) -> dict[tuple[str, str], Any]:
    """``{(name, type): detail}`` of every argument in a parsed message (variants unioned across repeats)."""
    sig: dict[tuple[str, str], Any] = {}

    def walk(nodes: tuple | list) -> None:
        for node in nodes:
            kind = node[0]
            if kind == "arg":
                sig.setdefault((node[1], "arg"), None)
            elif kind == "num":
                sig.setdefault((node[1], "num"), node[2])
            elif kind in ("plural", "select"):
                opts: dict[str, list] = node[2]
                variants = frozenset(opts) if kind == "select" else frozenset(s for s in opts if s.startswith("="))
                sig[(node[1], kind)] = sig.get((node[1], kind), frozenset()) | variants
                for body in opts.values():
                    walk(body)

    walk(ast)
    return sig


def _plural_categories(ast: tuple) -> set[str]:
    found: set[str] = set()

    def walk(nodes: tuple | list) -> None:
        for node in nodes:
            if node[0] in ("plural", "select"):
                if node[0] == "plural":
                    found.update(s for s in node[2] if not s.startswith("="))
                for body in node[2].values():
                    walk(body)

    walk(ast)
    return found


def _describe(sig: dict[tuple[str, str], Any]) -> str:
    return ", ".join(f"{n}:{t}" for n, t in sorted(sig))


def check_parity(i18n_dir: Path | None = None) -> list[str]:
    eng = engine()
    data, errors = load_catalogs(i18n_dir)
    for area, langs in data.items():
        en = langs.get("en", {})
        parsed_en: dict[str, tuple] = {}
        for lang in eng.LANGUAGES:
            catalog = langs.get(lang, {})
            allowed = set(eng.PLURAL_CATEGORIES_BY_LANG[lang])
            if lang != "en":
                for key in sorted(set(en) - set(catalog)):
                    errors.append(f"{area}/{lang}: missing key {key}")
                for key in sorted(set(catalog) - set(en)):
                    errors.append(f"{area}/{lang}: extra key {key} (not in English)")
            for key, text in catalog.items():
                where = f"{area}/{lang}: {key}"
                try:
                    ast = eng.parse_message(text)
                except eng.MessageSyntaxError as err:
                    errors.append(f"{where}: {err}")
                    continue
                bad = sorted(_plural_categories(ast) - allowed)
                if bad:
                    errors.append(f"{where}: plural categories {bad} are not CLDR cardinal categories of {lang}")
                if lang == "en":
                    parsed_en[key] = ast
                    continue
                if key not in en:
                    continue
                try:
                    en_ast = parsed_en.get(key) or eng.parse_message(en[key])
                except eng.MessageSyntaxError:
                    continue  # the English error is reported on its own
                want, got = message_signature(en_ast), message_signature(ast)
                if set(want) != set(got):
                    errors.append(f"{where}: arguments [{_describe(got)}] differ from English [{_describe(want)}]")
                    continue
                for name_type, detail in want.items():
                    if detail != got[name_type]:
                        errors.append(
                            f"{where}: {name_type[1]} '{name_type[0]}' variants/style differ from English "
                            f"({sorted(map(str, got[name_type] or []))} vs {sorted(map(str, detail or []))})"
                        )
    return errors


# ------------------------------------------------------------------------------------------------ HA catalog
def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Nested dict to ``{"a.b.c": leaf}``."""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.update(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    else:
        out[prefix] = obj
    return out


def _placeholders(text: str) -> set[str]:
    """Field names of a ``str.format`` template; raises ValueError for anything that is not one."""
    names: set[str] = set()
    for _literal, field, spec, conv in string.Formatter().parse(text):
        if field is None:
            continue
        if not field.isidentifier() or spec or conv:
            raise ValueError(f"unsupported placeholder {{{field}{':' + spec if spec else ''}}}")
        names.add(field)
    return names


def load_ha(translations_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """``{lang: flattened translations}`` for every ``translations/<lang>.json``."""
    translations_dir = translations_dir or TRANSLATIONS_DIR
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(translations_dir.glob("*.json")):
        out[path.stem] = flatten(_read_json(path))
    return out


def check_ha_parity(translations_dir: Path | None = None) -> list[str]:
    errors: list[str] = []
    try:
        catalogs = load_ha(translations_dir)
    except (OSError, ValueError) as err:
        return [f"translations/ unreadable: {err}"]
    en = catalogs.get("en")
    if en is None:
        return ["translations/en.json not found"]
    en_fields: dict[str, set[str]] = {}
    for lang, flat in catalogs.items():
        for key, value in flat.items():
            if not isinstance(value, str):
                continue
            try:
                fields = _placeholders(value)
            except ValueError as err:
                errors.append(f"translations/{lang}.json: {key}: {err}")
                continue
            if lang == "en":
                en_fields[key] = fields
        if lang == "en":
            continue
        for key in sorted(set(en) - set(flat)):
            errors.append(f"translations/{lang}.json: missing key {key}")
        for key in sorted(set(flat) - set(en)):
            errors.append(f"translations/{lang}.json: extra key {key} (not in en.json)")
        for key, value in flat.items():
            if key in en_fields and isinstance(value, str):
                try:
                    got = _placeholders(value)
                except ValueError:
                    continue
                if got != en_fields[key]:
                    errors.append(
                        f"translations/{lang}.json: {key}: placeholders {sorted(got)} != en {sorted(en_fields[key])}"
                    )
    return errors


# ------------------------------------------------------------------------------------------------ staleness
def load_locks(locks_dir: Path | None = None, area: str | None = None) -> dict[str, dict[str, dict[str, str]]]:
    """``{area: {lang: {key: hash}}}`` (all lock files, or just ``area``)."""
    locks_dir = locks_dir or LOCKS_DIR
    areas = [area] if area else [*engine().AREAS, HA_AREA]
    out: dict[str, dict[str, dict[str, str]]] = {}
    for name in areas:
        path = locks_dir / f"{name}.json"
        out[name] = _read_json(path) if path.is_file() else {}
    return out


def catalogs_by_area(
    i18n_dir: Path | None = None, translations_dir: Path | None = None
) -> dict[str, dict[str, dict[str, str]]]:
    """Every catalog (the 12 areas plus ``ha``) as ``{area: {lang: {key: text}}}``."""
    data, _errors = load_catalogs(i18n_dir)
    ha = load_ha(translations_dir)
    data[HA_AREA] = {lang: {k: v for k, v in flat.items() if isinstance(v, str)} for lang, flat in ha.items()}
    return data


def missing_keys(
    catalogs: dict[str, dict[str, dict[str, str]]], area: str | None = None, lang: str | None = None
) -> list[tuple[str, str, str]]:
    """``(area, lang, key)`` for every English key a language does not have."""
    out = []
    for name, langs in catalogs.items():
        if area and name != area:
            continue
        en = langs.get("en", {})
        for code, catalog in langs.items():
            if code == "en" or (lang and code != lang):
                continue
            out.extend((name, code, key) for key in sorted(en) if key not in catalog)
    return out


def stale_keys(
    catalogs: dict[str, dict[str, dict[str, str]]],
    locks: dict[str, dict[str, dict[str, str]]],
    area: str | None = None,
    lang: str | None = None,
) -> list[tuple[str, str, str, str]]:
    """``(area, lang, key, why)`` for every translation whose lock hash is not the current English hash."""
    out = []
    for name, langs in catalogs.items():
        if area and name != area:
            continue
        en = langs.get("en", {})
        for code, catalog in langs.items():
            if code == "en" or (lang and code != lang):
                continue
            locked = locks.get(name, {}).get(code, {})
            for key in sorted(catalog):
                if key not in en:
                    continue
                recorded = locked.get(key)
                if recorded is None:
                    out.append((name, code, key, "not locked"))
                elif recorded != en_hash(en[key]):
                    out.append((name, code, key, "English changed"))
    return out


def check_stale(
    locks_dir: Path | None = None, i18n_dir: Path | None = None, translations_dir: Path | None = None
) -> list[str]:
    try:
        catalogs = catalogs_by_area(i18n_dir, translations_dir)
        locks = load_locks(locks_dir)
    except (OSError, ValueError) as err:
        return [f"staleness lock unreadable: {err}"]
    return [
        f"{area}/{lang}: {key} is stale ({why}); re-translate, then `tools/i18n.py lock --area {area} --lang {lang}`"
        for area, lang, key, why in stale_keys(catalogs, locks)
    ]


def run_all(
    i18n_dir: Path | None = None, locks_dir: Path | None = None, translations_dir: Path | None = None
) -> dict[str, list[str]]:
    """Every guard, by name."""
    return {
        "parity": check_parity(i18n_dir),
        "ha parity": check_ha_parity(translations_dir),
        "stale": check_stale(locks_dir, i18n_dir, translations_dir),
    }
