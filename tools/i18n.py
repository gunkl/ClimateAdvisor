#!/usr/bin/env python3
"""Translation workflow CLI for Climate Advisor (Issue #1341).

    python tools/i18n.py missing [--area A] [--lang L]   English keys a language does not have yet
    python tools/i18n.py stale   [--area A] [--lang L]   translations written against older English
    python tools/i18n.py lock --area A --lang L|all      record "I translated against the current English"
    python tools/i18n.py check                           parity + HA parity + stale (exit 1 on any failure)

Lane workflow: write English, write the 15 translations, ``lock``, ``check``.
``--area ha`` addresses ``translations/<lang>.json`` (the Home Assistant catalog).
"""

from __future__ import annotations

import argparse
import json
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


def cmd_check(_args: argparse.Namespace) -> int:
    failed = False
    for name, errors in checks.run_all().items():
        print(f"[{'FAIL' if errors else 'ok'}] {name}")
        for err in errors:
            print(f"    {err}")
        failed = failed or bool(errors)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (("missing", cmd_missing), ("stale", cmd_stale), ("lock", cmd_lock)):
        p = sub.add_parser(name)
        p.add_argument("--area")
        p.add_argument("--lang")
        p.set_defaults(fn=fn)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
