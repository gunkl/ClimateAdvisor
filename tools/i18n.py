#!/usr/bin/env python3
"""Translation workflow CLI for Climate Advisor (Issue #1341).

    python tools/i18n.py missing [--area A] [--lang L]   English keys a language does not have yet
    python tools/i18n.py stale   [--area A] [--lang L]   translations written against older English
    python tools/i18n.py lock --area A --lang L|all      record "I translated against the current English"
    python tools/i18n.py check                           every guard: parity, HA parity, stale, length budgets,
                                                         the four ratchets (exit 1 on any failure)
    python tools/i18n.py tm-build                        rebuild i18n/tm.json (translation memory) from the catalogs
    python tools/i18n.py tm-fill --area A [--lang L]     fill missing keys whose English already has a translation
    python tools/i18n.py prompt --area A --lang L        translator prompt for the missing/stale keys
    python tools/i18n.py glossary [--area A] [--lang L]  WARNINGS: glossary term not used (never fails)
    python tools/i18n.py baseline --init|--update [NAME] create / lower the ratchet baselines (never raises)

Lane workflow: write English, write the 15 translations, ``lock``, ``check``.
``--area ha`` addresses ``translations/<lang>.json`` (the Home Assistant catalog).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_checks as checks  # noqa: E402


def _filters(args: argparse.Namespace) -> tuple[str | None, str | None]:
    eng = checks.engine()
    if args.area and args.area not in (*eng.AREAS, checks.HA_AREA):
        raise SystemExit(f"unknown area {args.area!r}; areas: {', '.join([*eng.AREAS, checks.HA_AREA])}")
    if args.lang and args.lang not in (*eng.LANGUAGES, "all"):
        raise SystemExit(f"unknown language {args.lang!r}; languages: {', '.join(eng.LANGUAGES)}")
    return args.area, (None if args.lang == "all" else args.lang)


def cmd_missing(args: argparse.Namespace) -> int:
    area, lang = _filters(args)
    rows = checks.missing_keys(checks.catalogs_by_area(), area, lang)
    for name, code, key in rows:
        print(f"{name}/{code}: missing {key}")
    print(f"{len(rows)} missing")
    return 0


def cmd_stale(args: argparse.Namespace) -> int:
    area, lang = _filters(args)
    rows = checks.stale_keys(checks.catalogs_by_area(), checks.load_locks(), area, lang)
    for name, code, key, why in rows:
        print(f"{name}/{code}: {key} ({why})")
    print(f"{len(rows)} stale")
    return 0


def write_lock(area: str, lang: str | None, locks_dir: Path | None = None, catalogs: dict | None = None) -> int:
    """Record the current English hashes for the keys present in ``lang`` (or every non-English language)."""
    catalogs = catalogs or checks.catalogs_by_area()
    langs = catalogs[area]
    en = langs.get("en", {})
    path = (locks_dir or checks.LOCKS_DIR) / f"{area}.json"
    lock = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    count = 0
    for code, catalog in langs.items():
        if code == "en" or (lang and code != lang):
            continue
        lock[code] = {key: checks.en_hash(en[key]) for key in sorted(catalog) if key in en}
        count += len(lock[code])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return count


def cmd_lock(args: argparse.Namespace) -> int:
    area, lang = _filters(args)
    if not area or not args.lang:
        raise SystemExit("lock needs --area A and --lang L|all")
    count = write_lock(area, lang)
    print(f"locked {count} translations in {area}" + (f"/{lang}" if lang else ""))
    return 0


# ------------------------------------------------------------------------------------------------ translation memory
LANGUAGE_NAMES = {
    "en": "English", "fr": "French", "es": "Spanish", "de": "German", "nl": "Dutch", "ja": "Japanese",
    "pt": "European Portuguese", "it": "Italian", "pl": "Polish", "uk": "Ukrainian", "ko": "Korean", "hi": "Hindi",
    "ar": "Arabic", "ru": "Russian", "zh-Hans": "Simplified Chinese", "tr": "Turkish",
}  # fmt: skip


def build_tm(catalogs: dict | None = None, locks: dict | None = None) -> dict[str, dict[str, str]]:
    """``{sha(en): {lang: translation}}`` from every APPROVED translation (its lock equals the current English).

    Identical English translated differently in several keys keeps the most frequent translation (ties: the first by
    area then key), so the memory is deterministic.
    """
    catalogs = catalogs or checks.catalogs_by_area()
    locks = locks or checks.load_locks()
    votes: dict[tuple[str, str], dict[str, list[str]]] = {}
    for area in sorted(catalogs):
        en = catalogs[area].get("en", {})
        for lang, catalog in sorted(catalogs[area].items()):
            if lang == "en":
                continue
            for key in sorted(catalog):
                if key in en and locks.get(area, {}).get(lang, {}).get(key) == checks.en_hash(en[key]):
                    votes.setdefault((checks.en_hash(en[key]), lang), {}).setdefault(catalog[key], []).append(key)
    tm: dict[str, dict[str, str]] = {}
    for (sha, lang), options in votes.items():
        best = max(options.items(), key=lambda kv: len(kv[1]))  # max keeps the first on ties (insertion order)
        tm.setdefault(sha, {})[lang] = best[0]
    return {sha: dict(sorted(langs.items())) for sha, langs in sorted(tm.items())}


def load_tm(path: Path | None = None) -> dict[str, dict[str, str]]:
    path = path or checks.TM_PATH
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _dump(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _catalog_file(area: str, lang: str) -> Path:
    if area == checks.HA_AREA:
        return checks.TRANSLATIONS_DIR / f"{lang}.json"
    return checks.I18N_DIR / area / f"{lang}.json"


def _set_nested(obj: dict, dotted: str, value: str) -> None:
    """Set ``a.b.c`` in a nested dict, preferring existing nodes (handles keys that themselves contain dots)."""
    parts = dotted.split(".")
    node = obj
    while len(parts) > 1:
        for n in range(len(parts) - 1, 0, -1):  # longest existing prefix first
            head = ".".join(parts[:n])
            if isinstance(node.get(head), dict):
                node, parts = node[head], parts[n:]
                break
        else:
            node = node.setdefault(parts[0], {})
            parts = parts[1:]
    node[parts[0]] = value


def tm_fill(area: str | None, lang: str | None, *, dry_run: bool = False) -> list[tuple[str, str, str]]:
    """Fill missing translations from the memory; record each as translated against the current English."""
    eng = checks.engine()
    tm = load_tm()
    catalogs = checks.catalogs_by_area()
    filled: list[tuple[str, str, str]] = []
    for name in [area] if area else [*eng.AREAS, checks.HA_AREA]:
        en = catalogs[name].get("en", {})
        for code in eng.LANGUAGES:
            if code == "en" or (lang and code != lang):
                continue
            if name == checks.HA_AREA and code not in catalogs[name]:
                continue  # never create a Home Assistant language file here
            have = catalogs[name].get(code, {})
            todo = {
                k: tm[checks.en_hash(en[k])][code]
                for k in sorted(en)
                if k not in have and code in tm.get(checks.en_hash(en[k]), {})
            }
            if not todo:
                continue
            filled.extend((name, code, k) for k in todo)
            if dry_run:
                continue
            path = _catalog_file(name, code)
            data = json.loads(path.read_text(encoding="utf-8"))
            for key, text in todo.items():
                if name == checks.HA_AREA:
                    _set_nested(data, key, text)
                else:
                    data[key] = text
            _dump(path, data)
            lock_path = checks.LOCKS_DIR / f"{name}.json"
            lock = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.is_file() else {}
            for key in todo:
                lock.setdefault(code, {})[key] = checks.en_hash(en[key])
            _dump(lock_path, lock)
    return filled


def cmd_tm_build(_args: argparse.Namespace) -> int:
    tm = build_tm()
    _dump(checks.TM_PATH, tm)
    print(f"translation memory: {len(tm)} English strings, {sum(len(v) for v in tm.values())} translations")
    return 0


def cmd_tm_fill(args: argparse.Namespace) -> int:
    area, lang = _filters(args)
    if not area:
        raise SystemExit("tm-fill needs --area A")
    rows = tm_fill(area, lang, dry_run=args.dry_run)
    for name, code, key in rows:
        print(f"{name}/{code}: filled {key}")
    print(f"{len(rows)} {'would be ' if args.dry_run else ''}filled from the translation memory")
    return 0


# ------------------------------------------------------------------------------------------------ translator prompt
def load_notes(area: str) -> dict[str, str]:
    """Translator notes: ``i18n/notes/<area>.json`` and/or ``frontend/i18n/<area>/_notes.json``."""
    notes: dict[str, str] = {}
    for path in (checks.NOTES_DIR / f"{area}.json", checks.I18N_DIR / area / "_notes.json"):
        if path.is_file():
            notes.update(json.loads(path.read_text(encoding="utf-8")))
    return notes


def build_prompt(area: str, lang: str, catalogs: dict | None = None, locks: dict | None = None) -> str:
    eng = checks.engine()
    catalogs = catalogs or checks.catalogs_by_area()
    locks = locks or checks.load_locks()
    en = catalogs[area].get("en", {})
    have = catalogs[area].get(lang, {})
    todo = {key: "missing" for key in sorted(en) if key not in have}
    todo.update({key: why for _a, _l, key, why in checks.stale_keys(catalogs, locks, area, lang)})
    notes = load_notes(area)
    budgets = checks.load_budgets()
    glossary = checks.parse_glossary()
    texts = " ".join(re.sub(r"\{[^{}]*\}", " ", en[k]) for k in todo)
    lines = [
        f"# Translate Climate Advisor strings: area `{area}`, into {LANGUAGE_NAMES[lang]} (`{lang}`)",
        "",
        "Reply with ONE JSON object `{key: translation}` for exactly the keys below, nothing else.",
        "",
        "## Rules",
        "- Keep every `{placeholder}` name exactly as in the English; never translate placeholder names.",
        "- Brand `Climate Advisor`, entity ids, `owner/repo`, `notify.xxx` and units (°F, °C, kW, min)",
        "  are never translated.",
    ]
    if area == checks.HA_AREA:
        lines.append("- Home Assistant catalog: plain `{name}` placeholders ONLY. No ICU (`plural`, `select`, `#`).")
    else:
        lines += [
            "- ICU subset: `{name}`, `{n, number}`, `{n, plural, ...}` and `{x, select, ...}` only;",
            "  every plural/select has `other`;",
            "  keep the same arguments and select variants as the English; `#` is the plural count.",
            f"- Plural categories for {lang} (CLDR cardinal; write all of them that the sentence needs, and `other`): "
            + ", ".join(eng.PLURAL_CATEGORIES_BY_LANG[lang]),
            "- One whole sentence per key; never split or join sentences.",
        ]
    style = glossary["style"].get(lang)
    lines += ["", f"## Style rules for {lang}"]
    if style:
        lines += [f"- {k}: {v}" for k, v in style.items()]
    else:
        lines.append("- (none recorded in GLOSSARY.md)")
    lines += ["", "## Glossary (use these approved terms)"]
    seen = False
    for term in glossary["terms"]:
        variants = checks._term_variants(term["en"])  # noqa: SLF001
        if any(re.search(rf"(?<![A-Za-z]){re.escape(v)}(?![A-Za-z])", texts, re.I) for v in variants):
            cell = term["cells"].get(lang, "")
            lines.append(f"- {term['en']} ({term['meaning']}): {cell}")
            seen = True
    if not seen:
        lines.append("- (no glossary term occurs in these strings)")
    n_missing = sum(v == "missing" for v in todo.values())
    lines += ["", f"## Keys ({len(todo)}: {n_missing} missing, {len(todo) - n_missing} stale)"]
    for key, why in todo.items():
        sig = checks.message_signature(eng.parse_message(en[key])) if area != checks.HA_AREA else {}
        lines += ["", f"### {key}", f"English: {en[key]}"]
        if sig:
            lines.append("Arguments: " + checks._describe(sig))  # noqa: SLF001
        if why != "missing" and key in have:
            lines.append(f"Previous translation ({why}): {have[key]}")
        lookup = key if area != checks.HA_AREA else f"ha:{key}"
        if lookup in budgets:
            lines.append(f"Maximum length: {budgets[lookup]} characters")
        if key in notes:
            lines.append(f"Translator note: {notes[key]}")
    return "\n".join(lines) + "\n"


def cmd_prompt(args: argparse.Namespace) -> int:
    area, lang = _filters(args)
    if not area or not lang:
        raise SystemExit("prompt needs --area A and --lang L")
    print(build_prompt(area, lang), end="")
    return 0


# ------------------------------------------------------------------------------------------------ glossary / baseline
def cmd_glossary(args: argparse.Namespace) -> int:
    area, lang = _filters(args)
    rows = checks.glossary_warnings(checks.catalogs_by_area(), area=area)
    if lang:
        rows = [r for r in rows if r.split(":", 1)[0].endswith(f"/{lang}")]
    for row in rows:
        print(f"WARNING {row}")
    print(f"{len(rows)} glossary warnings (inflection can legitimately change a term; review, do not force)")
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    if bool(args.init) == bool(args.update):
        raise SystemExit("baseline needs exactly one of --init or --update")
    names = [args.name] if args.name else list(checks.RATCHETS)
    for name in names:
        if name not in checks.RATCHETS:
            raise SystemExit(f"unknown ratchet {name!r}; ratchets: {', '.join(checks.RATCHETS)}")
        counts = checks.write_baseline(name, init=args.init)
        print(f"{name}: baseline written, total {sum(counts.values())} in {len(counts)} files")
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    failed = False
    for name, errors in checks.run_guards().items():
        print(f"[{'FAIL' if errors else 'ok'}] {name}")
        for err in errors:
            print(f"    {err}")
        failed = failed or bool(errors)
    for note in checks.ratchet_notes():
        print(f"NOTE {note}")
    print(
        f"glossary: {len(checks.glossary_warnings(checks.catalogs_by_area()))} warnings (python tools/i18n.py glossary)"
    )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (
        ("missing", cmd_missing),
        ("stale", cmd_stale),
        ("lock", cmd_lock),
        ("prompt", cmd_prompt),
        ("glossary", cmd_glossary),
    ):
        p = sub.add_parser(name)
        p.add_argument("--area")
        p.add_argument("--lang")
        p.set_defaults(fn=fn)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("tm-build").set_defaults(fn=cmd_tm_build)
    p = sub.add_parser("tm-fill")
    p.add_argument("--area")
    p.add_argument("--lang")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_tm_fill)
    p = sub.add_parser("baseline")
    p.add_argument("name", nargs="?")
    p.add_argument("--init", action="store_true")
    p.add_argument("--update", action="store_true")
    p.set_defaults(fn=cmd_baseline)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
