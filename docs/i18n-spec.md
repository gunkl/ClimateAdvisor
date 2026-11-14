<!-- Nav: ← [Project Instructions](00-PROJECT-INSTRUCTIONS.md) | → [i18n.js](../custom_components/climate_advisor/frontend/i18n.js) | ↔ [Development Guide](DEVELOPMENT.md) -->

# Multi-Language Support — Spec (Tier 3)

Issue #1341. Plan and recheck record: the #1341 plan (D1-D9, phases P0a-P5). This page is the contract the code is held to. Stage 1 (this page's first version) ships the runtime, the wiring and the guards; the strings move in later phases, area by area.

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| Which languages ship, and how is a code mapped? | Exactly 16: `en fr es de nl ja pt it pl uk ko hi ar ru zh-Hans tr`. Exact match, then case-insensitive, then the primary subtag (`pt-BR`→`pt`, `de-CH`→`de`, `zh`/`zh-CN`/`zh-SG`/`zh-Hans-*`→`zh-Hans`; `zh-TW`/`zh-HK`/`zh-Hant`→`en` for now), else `en`. | [Languages](#languages-and-resolution) |
| Which language does the dashboard use? | `?lang=`, then the parent frame's `hass.language`, then the status payload's `language`, then `navigator.language`, then `en`. The first non-empty candidate wins and is mapped to a shipped language. | [Languages](#languages-and-resolution) |
| Which language do notifications and entity states use? | `hass.config.language` (the language HA uses for entity names), mapped the same way. No Climate Advisor-specific option. | [Languages](#languages-and-resolution) |
| Where do translations live? | Two catalogs split by who renders the text: `translations/<lang>.json` (HA renders; plain `{name}` only) and `frontend/i18n/<area>/<lang>.json` (Climate Advisor renders; ICU subset). | [Catalogs](#catalogs-d1) |
| How do I add or change a user-visible string? | English first, then the 15 translations, then `tools/i18n.py lock`, then `tools/i18n.py check`. | [How to add a string](#how-to-add-a-string-and-the-lane-workflow) |
| What ICU syntax is allowed? | `{name}`, plural, select (with mandatory `other`), `number` with `::percent ::precision-integer ::.0 ::.00`; apostrophes as FormatJS; no tags, no offset, no selectordinal, no date/time, depth at most 2. | [ICU subset](#icu-subset) |
| How do Python and the dashboard stay identical? | One shared conformance fixture rendered by both (`tests/test_i18n_conformance.py`, `tests/i18n_conformance.mjs`) plus a plural-table drift test against `Intl.PluralRules`. | [Conformance](#conformance) |
| What does the dashboard JS API look like? | `window.CAI18N`: `init`, `lang`, `locale`, `dir`, `t`, `tw`, `fmt.*`, `esc`, `hass`. Loaded first on the new dashboard, `entry.html` and `prediction_accuracy.html`, never on classic `index.html`. | [Dashboard runtime](#dashboard-runtime) |
| Why does the panel iframe pick up a corrected translation? | The panel `?v=` hash covers `i18n.js`, the FormatJS bundle and every file under `frontend/i18n/`. | [Cache busting](#cache-busting) |

## Languages and resolution

- **Shipped (16):** `en fr es de nl ja pt it pl uk ko hi ar ru zh-Hans tr`. The code is the catalog file name. `frontend/i18n/index.json` (`{"languages": [...], "areas": [...]}`) is the single source of both lists for JS and Python.
- **Mapping:** see the first Anchors row. Python: `i18n.resolve_language`. JS: `CAI18N.resolveLanguage`. The same table drives both; a Node test pins the JS side (`tests/i18n_runtime.mjs`).
- **Backend (D2):** `i18n.hass_language(hass)` = `resolve_language(hass.config.language)`. The backend never mixes two languages.
- **Dashboard (D2):** per viewer: `?lang=` → parent `hass.language` (the one lookup, `CAI18N.hass()`, which reaches the `home-assistant` element in the parent or top window inside try/catch) → status payload `language` (the backend language, the one documented contract between the two runtimes) → `navigator.language` → `en`. An unsupported code resolves to its nearest shipped language or `en`; it does not fall through to the browser.
- `<html lang>` and `dir` are set on every page that loads `i18n.js` (`ar` → `rtl`, all others `ltr`), including `entry.html` and `prediction_accuracy.html`.
- Classic `index.html` stays English (owner Q2).

## Catalogs (D1)

1. **HA-rendered text: `translations/<lang>.json`**, exactly HA's method (config, options, selector, entity names and states, services, exceptions, issues). Plain `{name}` placeholders only; HA silently drops anything else. `strings.json` stays the byte-identical English source.
2. **Climate Advisor-rendered text: `frontend/i18n/<area>/<lang>.json`**: a flat object `{"<area>.<key>": "<ICU message>"}`; keys are always prefixed with their area name. Areas (one owner each in Stage 2): `common notify status timeline learning night activity briefing dashboard settings errors ai`. Served by the existing static path, read from disk by Python in the executor and cached. It holds no secrets and is served unauthenticated like the rest of `frontend/`.

- **Fallback is per key:** requested language → `en`. A key missing in `en` is a programming error: tests raise; production logs one warning and returns the key.
- **Stage 1 content:** every area directory has `en` and the 15 others; only `common` has real keys (mode names heat/cool/off/auto/fan_only, "unknown"), the other areas are `{}`.
- **Staleness lock** (outside the shipped package): `i18n/locks/<area>.json` and `i18n/locks/ha.json` hold `{"<lang>": {"<key>": "<sha1 of the English value, first 12 hex>"}}`. A translation is current when `lock[lang][key] == hash(en[key])`. `tools/i18n.py lock` records "I translated against this English".

## ICU subset

Identical semantics in Python (`i18n.py`) and FormatJS 10.7.16:

- `{name}`; `{n, plural, =0 {...} one {...} few {...} many {...} other {...}}` with `#` = the locale-formatted number; no `offset`.
- `{x, select, a {...} other {...}}`; `other` is mandatory in plural and select.
- `{n, number}` with the skeletons `::percent`, `::precision-integer`, `::.0`, `::.00` (nothing else; `::integer` is silently ignored by FormatJS 10.7.16, so it is banned). A plain `{n}` with a number renders as `String(n)` (not locale-formatted); only `{n, number}` and `#` format.
- Apostrophe quoting exactly as FormatJS: `''` is a literal `'`; `'{'` starts a quoted literal.
- No HTML or tags, nesting depth at most 2, no date/time/selectordinal.
- `ar`: the Python renderer wraps every rendered argument in U+2068 … U+2069 (notifications and states have no markup). The dashboard uses `<bdi>` in the DOM instead (Stage 2).
- Plural categories differ by language and the parity guard checks them per language: `ja ko zh-Hans` only `other`; `en de nl hi tr` `one|other`; `fr es pt it` `one|many|other`; `pl uk ru` `one|few|many|other`; `ar` `zero|one|two|few|many|other`.

## Messages, typed params and the wire format (D3)

Python builds `i18n.Msg(key, **params)`; `i18n.render(msg, lang)` renders it, `render_en` is for logs, stored reasons, GitHub text and AI input. The API adds a `{key, params}` sibling next to each prose field (the prose stays, rendered in the backend language). Wire form of the typed params (same names both sides):

| Python | Wire |
|---|---|
| plain `str` / `int` / `float` | as is (a plain `{n}` is `String(n)`; only `{n, number}` and `#` use the locale's separators) |
| `Temp(f, unit, decimals=0)` | `{"$t":"temp","f":72.3,"unit":"fahrenheit"\|"celsius","d":0}` |
| `TempDelta(f, unit, decimals=0)` | `{"$t":"tdelta",...}` (scale only, no offset) |
| `Time(dt)` / `Date(dt)` | `{"$t":"time"\|"date","ts":"<ISO 8601 with offset>"}` |
| `Duration(seconds)` | `{"$t":"dur","s":300}` |
| `ListOf(items, style="and")` | `{"$t":"list","items":[...],"style":"and"\|"or"}` |
| `Msg(key, **params)` nested | `{"$t":"msg","key":"...","params":{...}}` |
| `Code(key)` | `{"$t":"code","key":"common.mode.cool"}` (an enum rendered through its own catalog key) |

`Msg.to_wire()` → `{"key", "params"}`; `Msg.from_wire(d)` is the inverse. Temperatures travel as internal Fahrenheit plus the display unit (`"fahrenheit"`/`"celsius"`, the constants in `temperature.py`); the formatter converts. English output of `format_temp(..., lang=None)` is byte-identical to today's.

## Dashboard runtime

`frontend/i18n.js` defines `window.CAI18N`. It needs `frontend/intl-messageformat.iife.js` (FormatJS `intl-messageformat` 10.7.16, BSD-3-Clause, licence in `intl-messageformat.LICENSE.md`; the sourceMappingURL line is stripped; the global is the module namespace, so the code uses `IntlMessageFormat.IntlMessageFormat`). Both load first, before every other script.

| Member | Meaning |
|---|---|
| `init({statusLanguage, base})` | Resolves the language synchronously (so `lang`/`dir` and `<html lang dir>` are right at once), then fetches `index.json` and, for `en` and the chosen language, every area file. Returns a Promise that **always resolves** (a failed fetch leaves English/key fallbacks and one console warning); a later call (for example once the status payload is in) re-resolves and retries. |
| `lang`, `locale`, `dir` | Chosen shipped language; `hass.locale` when the parent has it (else `null`); `ltr`/`rtl`. |
| `t(key, params)` | Per-key English fallback; missing everywhere → the key plus one `console.warn`. |
| `tw(wire)` | Renders a `{key, params}` wire object; decodes typed params exactly as Python; a plain string passes through. |
| `fmt.number/time/date/dateTime/duration/list/temp/tempDelta` | `Intl.*`-based. `temp`/`tempDelta`: `Intl.NumberFormat(lang, {min/maxFractionDigits: d, useGrouping: false})` of the converted value (Celsius `(f-32)*5/9`, delta `f*5/9`), then `°F`/`°C` with no space. `list`: `Intl.ListFormat` conjunction/disjunction, long. `duration`: the `common.duration.min|hours|hours_frac` messages with the same thresholds as `i18n.format_duration` (minutes = max(1, round-half-even(s/60)); whole hours; else one decimal). `time`/`date`: `Intl.DateTimeFormat`, dashboard-owned. |
| `esc(s)` | The one HTML escaper. |
| `hass()` | The one parent-`hass` lookup (parent, then top window; never throws). |

Stage 1 adds the file and the `<script>` tags only; no page text uses `t()` yet, so every page behaves exactly as before. Stage 2 (L9) replaces the duplicated `haAuth`/`esc` helpers in `nd_*.js` with these. Prior art: lboue's closed PR #1562 (French), whose `t()`/`data-i18n`/`?lang=` design this adapts; its French strings are imported with credit in the strings phase.

### P0a results (headless Chromium, library loaded by a page in an iframe, as the panel does)

- `intl-messageformat` 10.7.16 IIFE: 91,494 bytes, sha256 `b200b1cf…c2a2260`, no `export` statement, no console errors.
- All 16 locales render plural, select, number (`21,5` in fr/de/…, `21.5` in en/ja/…) and date.
- A plural with no `other` is rejected at parse (`MISSING_OTHER_CLAUSE`), matching the parity guard.
- Chromium formats `ar` with Latin digits and inserts RLM marks in dates; the conformance fixture pins these exact outputs, so the Python renderer matches them.
- A real-browser smoke (Stage 1B): `new_dashboard.html` in an iframe whose parent exposes `hass.language="de"` → `CAI18N.lang === "de"`, no console errors; `"ar"` → `dir === "rtl"`; a missing language file warns once and falls back to English.

## Cache busting

The panel registers `entry.html?v=<hash>`. `_panel_asset_bytes` (`__init__.py`) hashes `entry.html`, `index.html`, `new_dashboard.html`, `i18n.js`, `intl-messageformat.iife.js` and every `*.json` under `frontend/i18n/` in sorted order, so a corrected translation changes the hash. The `StaticPathConfig` registration is unchanged. `tests/test_i18n_panel_wiring.py` pins that a changed or added catalog file changes the hash. Catalogs are loaded once in `async_setup_entry` (`await hass.async_add_executor_job(i18n.load_catalogs)`, skipped when `catalogs_loaded()`).

## Conformance

- `tests/fixtures/i18n_conformance.json`: rows `{"lang","msg","params"(wire),"expected"}`. `expected` is generated by FormatJS + `Intl` in Node (`tools/i18n_conformance_gen.mjs`, which records the Node and ICU versions).
- Python: `tests/test_i18n_conformance.py` (strips U+2068/U+2069 before comparing; a separate test asserts the `ar` wrapping). Node: `tests/i18n_conformance.mjs` loads the real `intl-messageformat.iife.js` and `i18n.js` into a vm sandbox (`tests/i18n_node_shim.mjs`) and renders every row through `CAI18N.tw`. `tests/i18n_runtime.mjs` checks resolution order, code mapping, per-key fallback, typed params, failed fetch and `dir`.
- `tests/test_i18n_plural_drift.py` compares Python's plural table with `Intl.PluralRules` (0..1000 and decimals).
- Times and dates are Python-owned per-language tables (browsers vary), pinned by `tests/test_i18n_format.py`, not cross-checked.
- CI: `node tests/i18n_runtime.mjs` and `node tests/i18n_conformance.mjs` run in the `lint` job of `validate.yml` (the one the `validate-and-test` gate collects), using the Node that ships on `ubuntu-latest`.

## Design decisions (D1-D9)

- **D1** Two catalogs split by who renders them (HA deletes ICU keys from `translations/`, so HA-rendered text cannot carry ICU). One English source per string. The existing label tables (`NEXT_AUTOMATION_LABELS`, `_MODE_SET_REASON_LABELS`, `OUTDOOR_SENSOR_REASON_LABELS`, `_WRITE_OUTCOME_LABELS`, `SENSOR_REASON_LABELS`, Settings metadata) keep their keys as catalog keys; only their English values move, and their tests are re-pointed at the catalog.
- **D2** Language resolution as above: backend from `hass.config.language`, dashboard per viewer.
- **D3** Messages, not English strings: an ICU subset, vendored FormatJS in JS, a small pure-Python renderer for exactly that subset (no pip requirement), `Msg` with typed params, whole-sentence messages (no code joins rendered sentences), codes for everything code compares (`status_code`, message keys, an explicit Settings `unit`), `{key, params}` siblings in the API.
- **D4** Stored data stays English for records: `event_log` keeps English `reason`/`detail` (via `render_en`) and adds `reason_key`/`reason_params` for display; legacy entries show their English and age out; the briefing stores its `Msg`; investigation reports stay in the language written; `last_update_error` stays English. `outcomes.py` is not touched.
- **D5** One formatter per concept: `temperature.format_temp`/`format_temp_delta` gain optional `lang`; `format_time`/`format_date`/`format_duration` live in `i18n.py` (no HA import); on the dashboard `i18n.js` owns resolution, `t()`, the `Intl` formatters, the one `hass` lookup and one `esc`.
- **D6** Entities and states use HA's own method: `_attr_translation_key`, names and state translations in `translations/`, prose-state sensors render a `Msg` in `hass.config.language` and expose stable `code`/`params` attributes.
- **D7** Workflow: an in-repo tool, no external service (Q5); machine drafts with a glossary and a translation memory; all keys ship translated (Q4); the `en-XA` pseudo-locale tests a branch before translations exist.
- **D8** Layout: physical CSS to logical, mirrored direction arrows, `<bdi>` around numbers/times/names, `overflow-wrap: anywhere` and `hyphens: auto`, `line-break: strict` for CJK.
- **D9** AI: the investigation skill is told to write occupant-facing sections in the language; `## SECTION` headers stay English; AI context uses `render_en`.

## Owner decisions (2026-11-14)

- **Q1** Portuguese ships as `pt` only; `pt-BR` maps to `pt`.
- **Q2** Classic `index.html` stays English only.
- **Q3** The Debug tab's chrome is translated; field names stay English.
- **Q4** No risk tiers or review gate; every string ships translated.
- **Q5** No Crowdin; an in-repo tool only.
- **Q6** `advises_opening` reads the message key; the 2 goldens that assert English card text are re-derived and re-signed (three-exercise protocol on each).

## Non-goals

hreflang and URL routing (a private panel); logs, brand name and email headers stay English; classic `index.html`; `zh-Hant` (maps to `en` for now).

## How to add a string and the lane workflow

1. Pick the area (`common notify status timeline learning night activity briefing dashboard settings errors ai`); the key is `<area>.<name>`.
2. Write the English value in `frontend/i18n/<area>/en.json` (HA-rendered text: `strings.json` and `translations/en.json`, plain `{name}` only). Use ICU for counts (`plural`) and grammatical variants (`select`); never join rendered sentences in code.
3. Use it: Python `from . import i18n` then `i18n.Msg("<area>.<name>", n=3)`; render with `i18n.render(msg, i18n.hass_language(self.hass))`. Dashboard: `CAI18N.t(...)` or `CAI18N.tw(wire)`. Never hard-code English at a sink; never compare against prose, compare codes.
4. Write the 15 translations in `<area>/<lang>.json` (same keys, same argument names, the language's own plural categories).
5. `python tools/i18n.py lock --area <area> --lang all` records that the translations were made against this English.
6. `python tools/i18n.py check` (parity, HA parity, staleness; `tools/validate.py` runs the same checks). Changing existing English later marks the key stale in every language until it is re-translated and re-locked: `python tools/i18n.py missing`, `stale`.

Summary: write English → 15 translations → `tools/i18n.py lock` → `check`.

## Tests

`tests/test_i18n_panel_wiring.py` (panel hash, status `language`), `tests/test_panel_entry.py` (panel URL, unchanged), the conformance and drift tests above, `tests/i18n_runtime.mjs`, `tests/i18n_conformance.mjs`.
