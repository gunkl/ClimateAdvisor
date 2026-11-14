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


# ================================================================================================ lane L1 guards
# Ratchets (hard-coded English at sinks, English-prose matching, JS DOM English, physical CSS), length budgets and the
# glossary check. The Python finders live in tests/helpers/ast_walk.py (the one sink list).
import html.parser  # noqa: E402
import re  # noqa: E402
from collections.abc import Callable  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from dataclasses import field as dc_field  # noqa: E402

BASELINES_DIR = REPO / "i18n" / "baselines"
BUDGETS_PATH = REPO / "i18n" / "budgets.json"
TM_PATH = REPO / "i18n" / "tm.json"
NOTES_DIR = REPO / "i18n" / "notes"
GLOSSARY_PATH = REPO / "i18n" / "GLOSSARY.md"
FRONTEND_DIR = COMPONENT_DIR / "frontend"
BASELINE_HINT = "python tools/i18n.py baseline --update"

_AST_WALK: Any = None


def ast_walk() -> Any:
    """``tests/helpers/ast_walk.py`` loaded by file path (``tests`` need not be importable)."""
    global _AST_WALK  # noqa: PLW0603
    if _AST_WALK is None:
        name = "climate_advisor_ast_walk_standalone"
        spec = importlib.util.spec_from_file_location(name, REPO / "tests" / "helpers" / "ast_walk.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # dataclasses resolve their module through sys.modules
        spec.loader.exec_module(module)
        _AST_WALK = module
    return _AST_WALK


Hit = Any  # ast_walk.Hit


# ------------------------------------------------------------------------------------------------ frontend scanning
VENDOR_JS = frozenset({"chart.umd.min.js", "chartjs-plugin-zoom.min.js", "hammer.min.js", "intl-messageformat.iife.js"})
# classic dashboard stays English (owner decision); it is also never right-to-left
CLASSIC_PAGES = frozenset({"index.html"})
STRICT_DEBUG_FILES = frozenset({"nd_debug.js"})  # debug view: field names are identifiers; only real prose counts


def frontend_sources(frontend_dir: Path | None = None) -> list[Path]:
    """Our own frontend ``.js``/``.html``/``.css`` (vendor libraries excluded; subdirectories are catalogs)."""
    root = frontend_dir or FRONTEND_DIR
    out = []
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix in (".js", ".html", ".css") and path.name not in VENDOR_JS:
            out.append(path)
    return out


def _blank(text: str) -> str:
    """Replace every non-newline character by a space (keeps offsets and line numbers)."""
    return re.sub(r"[^\n]", " ", text)


def _strip_js_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda m: _blank(m.group(0)), text, flags=re.S)
    return re.sub(r"(?<![:\"'\\])//[^\n]*", "", text)


def _strip_css_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda m: _blank(m.group(0)), text, flags=re.S)
    return re.sub(r"<!--.*?-->", lambda m: _blank(m.group(0)), text, flags=re.S)


def _script_view(path: Path, text: str) -> str:
    """The JavaScript of a file at its original offsets (``.js`` as is; HTML keeps only the inline ``<script>``s)."""
    if path.suffix == ".js":
        return _strip_js_comments(text)
    out = list(_blank(text))
    for m in re.finditer(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script>", text, flags=re.S | re.I):
        if "src=" in m.group("attrs").lower():
            continue
        out[m.start("body") : m.end("body")] = text[m.start("body") : m.end("body")]
    return _strip_js_comments("".join(out))


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


_WORDS = re.compile(r"[A-Za-z][A-Za-z'’]+")


def is_dom_english(body: str, *, html: bool = False, strict: bool = False) -> bool:
    """True when a string written to the DOM reads as English (no ``t()`` involved)."""
    s = re.sub(r"\$\{[^}]*\}", " ", body)
    if html:
        s = re.sub(r"<[^>]*>", " ", s)
        s = re.sub(r"&#?\w+;", " ", s)
    s = " ".join(s.split())
    words = _WORDS.findall(s)
    if not any(len(w) >= 3 for w in words):
        return False
    if " " in s:
        return len(words) >= 2
    if strict:
        return False
    return bool(re.fullmatch(r"[A-Z][A-Za-z'’-]{2,}", s))


_Q = r"(?P<q>['\"`])"
_BODY = r"(?P<body>(?:\\.|(?!(?P=q)).)*)(?P=q)"
_DOM_ASSIGN = re.compile(
    r"\.(?P<prop>textContent|innerText|innerHTML|outerHTML|title|placeholder|alt|ariaLabel)\s*\+?=\s*" + _Q + _BODY,
    re.S,
)
_DOM_CALL = re.compile(
    r"\b(?P<fn>createTextNode|insertAdjacentHTML|setAttribute)\s*\(\s*"
    r"(?:(?P<q0>['\"])(?P<first>[\w-]+)(?P=q0)\s*,\s*)?" + _Q + _BODY,
    re.S,
)
_CHART_KEY = re.compile(r"(?<![\w.])(?P<key>label|text)\s*:\s*" + _Q + _BODY, re.S)
_HTML_ATTR = re.compile(r"\b(?:title|alt|aria-label|placeholder)\s*=\s*\\?[\"']([^\"'<>]+)", re.I)
_I18N_ATTRS = ("title", "alt", "aria-label", "placeholder")


def _js_dom_hits(name: str, view: str, strict: bool) -> list[Hit]:
    H = ast_walk().Hit  # noqa: N806
    hits: list[Hit] = []

    def add(offset: int, sink: str, body: str, *, html: bool = False) -> None:
        if is_dom_english(body, html=html, strict=strict):
            hits.append(H(name, _line_of(view, offset), sink, " ".join(body.split())))
        if html:
            for attr in _HTML_ATTR.findall(body):
                if is_dom_english(attr, strict=strict):
                    hits.append(H(name, _line_of(view, offset), sink + ".attr", attr))

    for m in _DOM_ASSIGN.finditer(view):
        prop = m.group("prop")
        add(m.start(), prop, m.group("body"), html=prop in ("innerHTML", "outerHTML"))
    for m in _DOM_CALL.finditer(view):
        fn, first = m.group("fn"), m.group("first")
        if fn == "createTextNode" and first is None:
            add(m.start(), fn, m.group("body"))
        elif fn == "insertAdjacentHTML":
            add(m.start(), fn, m.group("body"), html=True)
        elif fn == "setAttribute" and first in _I18N_ATTRS:
            add(m.start(), f"setAttribute({first})", m.group("body"))
    for m in _CHART_KEY.finditer(view):
        add(m.start(), f"chart.{m.group('key')}", m.group("body"))
    return hits


class _StaticHtml(html.parser.HTMLParser):
    """Visible text and the title/alt/aria-label/placeholder attributes of static HTML not marked ``data-i18n*``."""

    def __init__(self, name: str, strict: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.name, self.strict = name, strict
        self.hits: list[Hit] = []
        self.stack: list[tuple[str, bool]] = []
        self.skip = 0
        self.void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        marked = any(k.startswith("data-i18n") for k, _v in attrs)
        if tag in ("script", "style"):
            self.skip += 1
        for key, value in attrs:
            if (
                key in _I18N_ATTRS
                and value
                and not marked
                and not self._covered()
                and is_dom_english(value, strict=self.strict)
            ):
                self.hits.append(ast_walk().Hit(self.name, self.getpos()[0], f"html.{key}", value))
        if tag not in self.void:
            self.stack.append((tag, marked))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def _covered(self) -> bool:
        return any(marked for _t, marked in self.stack)

    def handle_data(self, data: str) -> None:
        if self.skip or self._covered() or not data.strip():
            return
        if is_dom_english(data, strict=self.strict):
            self.hits.append(ast_walk().Hit(self.name, self.getpos()[0], "html.text", " ".join(data.split())))


def find_js_dom_english(frontend_dir: Path | None = None) -> list[Hit]:
    """English written to DOM sinks (and static HTML text) without ``t()``/``data-i18n`` in the new dashboard pages."""
    hits: list[Hit] = []
    for path in frontend_sources(frontend_dir):
        if path.suffix == ".css" or path.name in CLASSIC_PAGES:
            continue
        name = f"frontend/{path.name}"
        text = path.read_text(encoding="utf-8")
        strict = path.name in STRICT_DEBUG_FILES
        hits.extend(_js_dom_hits(name, _script_view(path, text), strict))
        if path.suffix == ".html":
            parser = _StaticHtml(name, strict)
            parser.feed(_strip_css_comments(text))
            parser.close()
            hits.extend(parser.hits)
    return sorted(set(hits), key=lambda h: (h.file, h.line, h.sink, h.text))


_JS_PROSE = (
    (
        "startsWith/endsWith/includes",
        re.compile(r"\.(?:startsWith|endsWith|includes)\(\s*(?P<q>['\"`])(?P<t>(?:\\.|(?!(?P=q)).)*)(?P=q)"),
    ),
    ("compare", re.compile(r"[!=]==?\s*(?P<q>['\"`])(?P<t>(?:\\.|(?!(?P=q)).)*)(?P=q)")),
    (
        "regex",
        re.compile(
            r"/(?P<t>(?:\\.|[^/\n\\])*)/[gimsuy]*\s*\.test\(|\.(?:match|search)\(\s*/(?P<t2>(?:\\.|[^/\n\\])*)/"
        ),
    ),
)


def find_js_prose_comparisons(frontend_dir: Path | None = None) -> list[Hit]:
    """JS ``startsWith``/``includes``/``===``/regex against literal English prose."""
    hits: list[Hit] = []
    for path in frontend_sources(frontend_dir):
        if path.suffix == ".css" or path.name in CLASSIC_PAGES:
            continue
        name = f"frontend/{path.name}"
        view = _script_view(path, path.read_text(encoding="utf-8"))
        for label, pattern in _JS_PROSE:
            for m in pattern.finditer(view):
                text = m.group("t") or (m.groupdict().get("t2") or "")
                if ast_walk().is_english_prose(text, 2):
                    hits.append(ast_walk().Hit(name, _line_of(view, m.start()), f"js.{label}", text))
    return sorted(set(hits), key=lambda h: (h.file, h.line, h.sink, h.text))


_PHYSICAL_CSS = re.compile(
    r"(?<![\w-])(?:margin|padding)-(?:left|right)\s*:"
    r"|(?<![\w-])border-(?:left|right)(?:-(?:width|style|color))?\s*:"
    r"|(?<![\w-])border-(?:top|bottom)-(?:left|right)-radius\s*:"
    r"|(?<![\w-])(?:left|right)\s*:\s*(?!\s*(?:left|right)\b)[-\w.%(]"
    r"|(?<![\w-])text-align\s*:\s*(?:left|right)\b"
    r"|(?<![\w-])float\s*:\s*(?:left|right)\b"
    r"|(?<![\w-])clear\s*:\s*(?:left|right)\b"
    r"|\b(?:margin|padding)(?:Left|Right)\b"
    r"|\bstyle\.(?:left|right)\s*=",
    re.I,
)


def find_physical_css(frontend_dir: Path | None = None) -> list[Hit]:
    """Physical (non-logical) CSS properties in frontend CSS, HTML and JS style strings."""
    hits: list[Hit] = []
    for path in frontend_sources(frontend_dir):
        if path.name in CLASSIC_PAGES:
            continue
        name = f"frontend/{path.name}"
        text = _strip_css_comments(path.read_text(encoding="utf-8"))
        if path.suffix == ".js":
            text = _strip_js_comments(text)
        for m in _PHYSICAL_CSS.finditer(text):
            hits.append(ast_walk().Hit(name, _line_of(text, m.start()), "css", " ".join(m.group(0).split())))
    return sorted(set(hits), key=lambda h: (h.file, h.line, h.sink, h.text))


# ------------------------------------------------------------------------------------------------ ratchets
def find_py_literals() -> list[Hit]:
    return ast_walk().iter_package(ast_walk().find_literals_at_sinks)


def find_english_matching() -> list[Hit]:
    return ast_walk().iter_package(ast_walk().find_prose_comparisons) + find_js_prose_comparisons()


# name -> (finder, one-line description). The name is the baseline file (i18n/baselines/<name>.json).
RATCHETS: dict[str, tuple[Callable[[], list[Hit]], str]] = {
    "py_literals": (find_py_literals, "hard-coded English at Python occupant sinks"),
    "english_matching": (find_english_matching, "code comparing English prose (Python and JS)"),
    "js_dom_literals": (find_js_dom_english, "hard-coded English at JS/HTML DOM sinks (classic index.html excluded)"),
    "physical_css": (find_physical_css, "physical CSS properties (use logical ones)"),
}


@dataclass
class RatchetResult:
    errors: list[str] = dc_field(default_factory=list)
    notes: list[str] = dc_field(default_factory=list)
    counts: dict[str, int] = dc_field(default_factory=dict)


def baseline_path(name: str, baselines_dir: Path | None = None) -> Path:
    return (baselines_dir or BASELINES_DIR) / f"{name}.json"


def load_baseline(name: str, baselines_dir: Path | None = None) -> dict[str, int] | None:
    path = baseline_path(name, baselines_dir)
    if not path.is_file():
        return None
    return {k: int(v) for k, v in _read_json(path).get("files", {}).items()}


def baseline_ratchet(
    name: str,
    finder: Callable[[], list[Hit]],
    baseline: dict[str, int] | None = None,
    baselines_dir: Path | None = None,
) -> RatchetResult:
    """Counts per file may only fall. A file may not gain hits even while another loses some."""
    result = RatchetResult()
    hits = finder()
    by_file: dict[str, list[Hit]] = {}
    for hit in hits:
        by_file.setdefault(hit.file, []).append(hit)
    result.counts = {f: len(h) for f, h in sorted(by_file.items())}
    if baseline is None:
        baseline = load_baseline(name, baselines_dir)
    if baseline is None:
        result.errors.append(
            f"baseline i18n/baselines/{name}.json missing; create it with `python tools/i18n.py baseline --init`"
        )
        return result
    for file, count in result.counts.items():
        allowed = baseline.get(file, 0)
        if count > allowed:
            shown = "; ".join(str(h) for h in by_file[file][:6])
            more = "" if count <= 6 else f"; ... {count - 6} more"
            result.errors.append(
                f"{file}: {count} hits > baseline {allowed} ({RATCHETS.get(name, (None, name))[1]}). "
                f"Use the catalog (i18n.Msg / CAI18N.t) instead. Hits: {shown}{more}"
            )
    fallen = {f: (baseline[f], result.counts.get(f, 0)) for f in baseline if result.counts.get(f, 0) < baseline[f]}
    if fallen:
        detail = ", ".join(f"{f} {old}->{new}" for f, (old, new) in sorted(fallen.items())[:8])
        result.notes.append(f"{name}: counts fell ({detail}); lower the baseline with `{BASELINE_HINT}`")
    if not result.counts and not result.errors:
        result.notes.append(f"{name}: zero hits; this guard is now absolute")
    return result


def run_ratchets(baselines_dir: Path | None = None) -> dict[str, RatchetResult]:
    return {
        name: baseline_ratchet(name, finder, baselines_dir=baselines_dir) for name, (finder, _d) in RATCHETS.items()
    }


def write_baseline(name: str, *, init: bool, baselines_dir: Path | None = None) -> dict[str, int]:
    """Write a baseline. ``init`` records the current counts; otherwise counts only ever go down (per file)."""
    finder = RATCHETS[name][0]
    counts = ast_walk().counts_by_file(finder())
    path = baseline_path(name, baselines_dir)
    if init:
        if path.is_file():
            raise FileExistsError(f"{path} exists; use --update to lower it")
    else:
        old = load_baseline(name, baselines_dir) or {}
        counts = {f: min(c, old[f]) if f in old else 0 for f, c in counts.items()}
        counts = {f: c for f, c in counts.items() if c}
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"description": RATCHETS[name][1], "total": sum(counts.values()), "files": counts}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return counts


# ------------------------------------------------------------------------------------------------ rendering samples
PLURAL_SAMPLES = (0, 1, 2, 3, 5, 11, 21, 100)


def sample_param_sets(text: str, *, arg_value: str = "Sample") -> list[dict[str, Any]]:
    """Representative parameter sets for one message: a base set plus one variation per plural/select/number.

    Text arguments take ``arg_value``; numbers 3; every plural varies over ``PLURAL_SAMPLES`` and every select over
    its variants plus an unmatched value (the ``other`` branch).
    """
    sig = message_signature(engine().parse_message(text))
    base: dict[str, Any] = {}
    for (name, kind), _detail in sig.items():
        base[name] = arg_value if kind == "arg" else "other_variant" if kind == "select" else 3
    sets = [dict(base)]
    for (name, kind), detail in sig.items():
        if kind == "plural":
            sets.extend({**base, name: n} for n in PLURAL_SAMPLES)
        elif kind == "select":
            sets.extend({**base, name: v} for v in sorted(detail or ()))
    return sets


def longest_rendering(text: str, lang: str) -> int:
    """Length of the longest rendering of ``text`` over its sample parameter sets (text arguments empty)."""
    eng = engine()
    return max(len(eng.format_message(text, lang, p)) for p in sample_param_sets(text, arg_value=""))


# ------------------------------------------------------------------------------------------------ length budgets
def load_budgets(path: Path | None = None) -> dict[str, int]:
    path = path or BUDGETS_PATH
    return {k: int(v) for k, v in _read_json(path).items()} if path.is_file() else {}


def check_budgets(
    budgets_path: Path | None = None, i18n_dir: Path | None = None, translations_dir: Path | None = None
) -> list[str]:
    """Per-key maximum length in every language, English included: characters of the longest rendering (plural
    counts and select variants sampled, text arguments empty; Home Assistant values are measured as written).

    Keys are ``<area>.<name>`` for the dashboard catalogs and ``ha:<dotted.key>`` for ``translations/``.
    """
    try:
        budgets = load_budgets(budgets_path)
        catalogs = catalogs_by_area(i18n_dir, translations_dir)
    except (OSError, ValueError) as err:
        return [f"budgets unreadable: {err}"]
    errors: list[str] = []
    for key, limit in sorted(budgets.items()):
        area, _, bare = key.partition(":") if key.startswith("ha:") else (key.split(".", 1)[0], "", key)
        area = HA_AREA if key.startswith("ha:") else area
        langs = catalogs.get(area)
        if langs is None or bare not in langs.get("en", {}):
            errors.append(f"budgets: {key} is not an English catalog key (remove or fix it)")
            continue
        for lang, catalog in langs.items():
            text = catalog.get(bare)
            if text is None:
                continue
            try:
                size = len(text) if area == HA_AREA else longest_rendering(text, lang)
            except Exception as err:  # noqa: BLE001 - a broken message is reported by parity; do not hide it here
                errors.append(f"{area}/{lang}: {bare} could not be measured: {err}")
                continue
            if size > limit:
                errors.append(f"{area}/{lang}: {bare} renders up to {size} chars, budget {limit}")
    return errors


# ------------------------------------------------------------------------------------------------ glossary
_GENDER = re.compile(r"\s*\((?:[mfn](?:/[mfn])?)\)")
_UNSURE = re.compile(r"\(\?\)|→")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_glossary(path: Path | None = None) -> dict[str, Any]:
    """``{"terms": [...], "style": {lang: {...}}}`` from ``i18n/GLOSSARY.md``.

    A term is ``{"en", "meaning", "section", "cells": {lang: raw}, "approved": {lang: text}}``; a cell is approved
    unless it carries ``(?)`` or an arrow (not confident). Gender marks are stripped from approved text.
    """
    path = path or GLOSSARY_PATH
    langs = engine().LANGUAGES
    terms: list[dict[str, Any]] = []
    style: dict[str, dict[str, str]] = {}
    section = ""
    header: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#"):
            section = raw.lstrip("# ").strip()
            header = []
            continue
        if not raw.startswith("|"):
            continue
        cells = _cells(raw)
        if set("".join(cells)) <= set("-: "):
            continue
        if cells[0] in ("en", "lang"):
            header = cells
            continue
        if not header or len(cells) != len(header):
            continue
        if header[0] == "lang":
            style[cells[0]] = dict(zip(header[1:], cells[1:], strict=True))
            continue
        row = dict(zip(header, cells, strict=True))
        approved = {}
        for lang in langs:
            cell = row.get(lang) if lang != "en" else None  # row["en"] is the term itself
            if cell and not _UNSURE.search(cell):
                approved[lang] = _GENDER.sub("", cell).strip()
        terms.append(
            {
                "en": cells[0],
                "meaning": row.get("meaning", ""),
                "section": section,
                "cells": {lang: row[lang] for lang in langs if lang in row},
                "approved": approved,
            }
        )
    return {"terms": terms, "style": style}


def _term_variants(en: str) -> list[str]:
    base = re.sub(r"\([^)]*\)", " ", en)
    return [v.strip() for v in base.split(" / ") if len(v.strip()) >= 4]


def glossary_warnings(
    catalogs: dict[str, dict[str, dict[str, str]]], glossary: dict[str, Any] | None = None, area: str | None = None
) -> list[str]:
    """WARNINGS (never failures): an English value contains a glossary term, but a language's value lacks its
    approved translation. Inflection makes an exact match unreliable, so this only points a reviewer at the key."""
    glossary = glossary or parse_glossary()
    out: list[str] = []
    for name, langs in catalogs.items():
        if area and name != area:
            continue
        en = langs.get("en", {})
        for term in glossary["terms"]:
            patterns = [
                re.compile(rf"(?<![A-Za-z]){re.escape(v)}(?![A-Za-z])", re.I) for v in _term_variants(term["en"])
            ]
            if not patterns:
                continue
            for key, text in sorted(en.items()):
                plain = re.sub(r"\{[^{}]*\}", " ", text)
                if not any(p.search(plain) for p in patterns):
                    continue
                for lang, approved in term["approved"].items():
                    value = langs.get(lang, {}).get(key)
                    if value is not None and approved.lower() not in value.lower():
                        out.append(f"{name}/{lang}: {key}: expected glossary term {approved!r} for {term['en']!r}")
    return out


# ------------------------------------------------------------------------------------------------ everything
def run_guards(
    i18n_dir: Path | None = None,
    locks_dir: Path | None = None,
    translations_dir: Path | None = None,
    baselines_dir: Path | None = None,
) -> dict[str, list[str]]:
    """Every guard, by name (the Stage 1 three plus the budgets and the four ratchets)."""
    out = run_all(i18n_dir, locks_dir, translations_dir)
    out["length budgets"] = check_budgets(None, i18n_dir, translations_dir)
    for name, result in run_ratchets(baselines_dir).items():
        out[f"ratchet {name}"] = result.errors
    return out


def ratchet_notes(baselines_dir: Path | None = None) -> list[str]:
    """Non-failing hints (counts fell: lower the baseline)."""
    return [n for result in run_ratchets(baselines_dir).values() for n in result.notes]
