<!-- Nav: ← [Project Instructions](00-PROJECT-INSTRUCTIONS.md) | → [i18n.js](../custom_components/climate_advisor/frontend/i18n.js) | ↔ [Development Guide](DEVELOPMENT.md) -->

# Multi-Language Support — Spec (Tier 3)

Issue #1341. Plan and recheck record: the #1341 plan (D1-D9, phases P0a-P5). This page is the contract the code is held to. Stage 1 (this page's first version) ships the runtime, the wiring and the guards; the strings move in later phases, area by area.

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| Which languages ship, and how is a code mapped? | Exactly 17: `en fr es de nl ja pt it pl uk ko hi ar ru zh-Hans zh-Hant tr`. Exact match, then case-insensitive, then the primary subtag (`pt-BR`→`pt`, `de-CH`→`de`, `zh`/`zh-CN`/`zh-SG`/`zh-Hans-*` (any region, so `zh-Hans-TW` too)→`zh-Hans`; `zh-TW`/`zh-HK`/`zh-MO`/`zh-Hant`→`zh-Hant`, Issue #1656), else `en`. | [Languages](#languages-and-resolution) |
| Which language does the dashboard use? | `?lang=`, then the parent frame's `hass.language`, then the status payload's `language`, then `navigator.language`, then `en`. The first non-empty candidate wins and is mapped to a shipped language. | [Languages](#languages-and-resolution) |
| Which language do notifications and entity states use? | `hass.config.language` (the language HA uses for entity names), mapped the same way. No Climate Advisor-specific option. | [Languages](#languages-and-resolution) |
| Where do translations live? | Two catalogs split by who renders the text: `translations/<lang>.json` (HA renders; plain `{name}` only) and `frontend/i18n/<area>/<lang>.json` (Climate Advisor renders; ICU subset). | [Catalogs](#catalogs-d1) |
| How do I add or change a user-visible string? | English first, then the 16 translations, then `tools/i18n.py lock`, then `tools/i18n.py check`. | [How to add a string](#how-to-add-a-string-and-the-lane-workflow) |
| What ICU syntax is allowed? | `{name}`, plural, select (with mandatory `other`), `number` with exactly `::percent ::precision-integer ::.0 ::.00`; apostrophes, argument whitespace and `<` as FormatJS; no tags, no offset, no selectordinal, no date/time, depth at most 2. | [ICU subset](#icu-subset) |
| How do Python and the dashboard stay identical? | One shared conformance fixture rendered by both (`tests/test_i18n_conformance.py`, `tests/i18n_conformance.mjs`; 1,989 rows including typed params, fallbacks, bad parameters, times and dates), one shared language-resolution table, and a plural-table drift test against `Intl.PluralRules`. | [Conformance](#conformance) |
| What does the dashboard JS API look like? | `window.CAI18N`: `init`, `lang`, `locale`, `dir`, `t`, `tw`, `fmt.*`, `esc`, `hass`. Loaded first on the new dashboard, `entry.html` and `prediction_accuracy.html`, never on classic `index.html`. | [Dashboard runtime](#dashboard-runtime) |
| Why does the panel iframe pick up a corrected translation? | The panel `?v=` hash covers `i18n.js`, the FormatJS bundle and every file under `frontend/i18n/`. | [Cache busting](#cache-busting) |

## Languages and resolution

- **Shipped (17):** `en fr es de nl ja pt it pl uk ko hi ar ru zh-Hans zh-Hant tr` (`zh-Hant` is Traditional Chinese written for Taiwan, Issue #1656). The code is the catalog file name. `frontend/i18n/index.json` (`{"languages": [...], "areas": [...]}`) is the single source of both lists for JS and Python.
- **Mapping:** see the first Anchors row. Python: `i18n.resolve_language`. JS: `CAI18N.resolveLanguage`. The same table drives both: `tests/fixtures/i18n_language_resolution.json` (`[input, shipped language]` pairs, `null`/non-string included) is read by `tests/test_i18n_engine.py` and by `tests/i18n_runtime.mjs`, so the two cannot disagree. An explicit `Hans` script wins over any region (`zh-Hans-TW` is Simplified); any `Hant`/`TW`/`HK`/`MO` marker without it gives `zh-Hant`.
- **Language lists:** `index.json` is the source; `i18n.py` and the offline-fallback arrays in `i18n.js` are each checked against it (`tests/test_i18n_engine.py::test_index_json_is_the_single_source_of_the_lists`, `::test_js_language_and_area_lists_match_index_json`), and `tools/i18n_conformance_gen.mjs` reads it.
- **Backend (D2):** `i18n.hass_language(hass)` = `resolve_language(hass.config.language)`. The backend never mixes two languages.
- **Dashboard (D2):** per viewer: `?lang=` → parent `hass.language` (the one lookup, `CAI18N.hass()`, which reaches the `home-assistant` element in the parent or top window inside try/catch) → status payload `language` (the backend language, the one documented contract between the two runtimes) → `navigator.language` → `en`. An unsupported code resolves to its nearest shipped language or `en`; it does not fall through to the browser.
- `<html lang>` is set on every page that loads `i18n.js`, including `entry.html` and `prediction_accuracy.html`. `<html dir>` is `rtl` for `ar` and `ltr` for every other language (`RTL_LAYOUT_READY` in `i18n.js` is `true`: the dashboard CSS uses logical properties, enforced by the `physical_css` ratchet; the charts' own time axis stays left to right inside an `ltr` chart stack); `CAI18N.dir` reports the language's direction so text code can isolate values.
- Classic `index.html` stays English (owner Q2).

### Traditional Chinese (Taiwan), `zh-Hant` (Issue #1656)

- **Written for Taiwan from the English, never converted from `zh-Hans`.** Wording follows Home Assistant's own zh-Hant translations wherever HA has the term (glossary column `zh-Hant`, "Per-language style" row), so Climate Advisor reads like the rest of the user's Home Assistant: 溫控器, 暖氣/冷氣, 感測器, 設定, 預設, 排程, 離家, 您.
- **Guard** (`zh-Hant wording` in `tools/i18n.py check` and `tools/validate.py`): fails on any Simplified-only character or mainland-only term in a `zh-Hant` value, dashboard or HA catalog. The lists are in `i18n/zh_hant_terms.json`; each entry was checked against Home Assistant's zh-Hant translations. Context-dependent words (設置, 信息, 打開, …) are deliberately left out.
- **Corrections from readers are welcome.** Open an issue with the label `translation: zh-Hant`, naming the screen or the catalog key (for example `status.card.windows_open`, or `ha:entity.sensor.day_type.name`) and the better wording. A list of `key → wording` in one issue is fine. The fix updates `frontend/i18n/<area>/zh-Hant.json` or `translations/zh-Hant.json`, then `python tools/i18n.py lock --area <area> --lang zh-Hant` and `check`. If a correction changes a glossary term, the glossary cell changes too, so later strings follow it.

## Catalogs (D1)

1. **HA-rendered text: `translations/<lang>.json`**, exactly HA's method (config, options, selector, entity names and states, services, exceptions, issues). Plain `{name}` placeholders only; HA silently drops anything else. `strings.json` stays the byte-identical English source.
2. **Climate Advisor-rendered text: `frontend/i18n/<area>/<lang>.json`**: a flat object `{"<area>.<key>": "<ICU message>"}`; keys are always prefixed with their area name. Areas (one owner each in Stage 2): `common notify status timeline learning night activity briefing dashboard settings errors ai`. Served by the existing static path, read from disk by Python in the executor and cached. It holds no secrets and is served unauthenticated like the rest of `frontend/`.

- **Fallback is per key:** requested language → `en`, and the English pattern is then formatted with the REQUESTED language's number, plural and list rules (Python and JS do the same; fixture rows pin it). A key missing in `en` is a programming error: tests raise (strict mode); production logs one warning and returns the key.
- **Rendering never raises** (non-strict, i.e. production): a translation that does not parse or format falls back to the English message rendered in English (parameters decoded in English), then to the key; a parameter that cannot be rendered (`None`, NaN seconds, a non-datetime, a malformed wire object, a wire object with an unknown `$t`) shows as an em dash (U+2014) and the rest of the sentence renders; a message with no key renders as an empty string. Each case logs one warning per key. Strict mode (`i18n.set_strict(True)`, on for the whole unit and real-HA test sessions) raises instead. `async_setup_entry` also treats a failed catalog load as non-fatal (one error with the reason, then the fallbacks above).
- **Stage 1 content:** every area directory has `en` and the 15 others (16 since `zh-Hant`); only `common` has real keys (mode names heat/cool/off/auto/fan_only, "unknown"), the other areas are `{}`.
- **Staleness lock** (outside the shipped package): `i18n/locks/<area>.json` and `i18n/locks/ha.json` hold `{"<lang>": {"<key>": "<sha1 of the English value, first 12 hex>"}}`. A translation is current when `lock[lang][key] == hash(en[key])`. `tools/i18n.py lock` records "I translated against this English".

## HA-rendered text (P1: entities, selectors, services, exceptions)

Everything Home Assistant itself renders is a key in `strings.json` (byte-identical to `translations/en.json`), with plain `{name}` placeholders only. `tools/validate.py` (`check_ha_translations`) enforces hassfest's slug and placeholder rules plus the code-to-catalog cross-checks.

| Surface | Where the text is | Rule |
|---|---|---|
| Entity names | `entity.<platform>.<translation_key>.name` | `_attr_translation_key` = the unique-id suffix (`day_type`, `automation_enabled`, ...) and `_attr_has_entity_name = True`; **never** `_attr_name` (it overrides the translation). The English names equal the old hard-coded ones, so an English install keeps its friendly names and entity ids. |
| Code-valued states | `entity.sensor.<key>.state.<value>` | Only values that are hassfest slugs (`hot`, `circuit_open`). A state with spaces or punctuation (`running (manual override)`) is prose and is rendered through a `Msg` instead. |
| Prose-valued states | `i18n.render(Msg, i18n.hass_language(hass))` in `sensor.py` (`_prose`) | A `str` passes through; a `Msg`/`Code` the coordinator provides is rendered in HA's language. Each prose sensor also exposes stable `code` and `params` attributes (`code` is None for legacy plain prose). |
| Option lists | `selector.<translation_key>.options.<value>` | `SelectSelectorConfig(options=[plain values], translation_key=...)`. A dynamic label (a schedule summary) keeps its own label and only its fixed words are translated. |
| Words built in Python (schedule summary, key and token status) | `common.<key>` and `selector.weekday` | Read with `config_flow._async_texts(hass)` through HA's translation cache (English underneath); `_text(texts, key, **placeholders)` fills plain placeholders. |
| Services | `services.<service>.name`, `.description`, `.fields.<field>.name/.description` | `services.yaml` holds no text, only field structure, defaults, examples and `translation_key`s. |
| Exceptions | `exceptions.<key>.message` | `ServiceValidationError(translation_domain=DOMAIN, translation_key=..., translation_placeholders=...)`. HA's own `str()` of the error is the English text. |

Entity ids: HA builds an entity id once, when the entity is first registered, from the entity name in the language HA is set to if that language is one of HA's native-entity-id languages (de, fr, es, it, nl, pt, pl, tr of our 16), else from the English name. Existing installs keep their ids; a fresh install in a native-id language gets ids in that language. Docs and tools therefore never assume `sensor.climate_advisor_*`: they look up by unique id (`<config entry id>_<key>`), see `tools/ha_logs.py --entity ca:<key>` and `tools/thermal_health.py`.

## ICU subset

Identical semantics in Python (`i18n.py`) and FormatJS 10.7.16:

- `{name}`; `{n, plural, =0 {...} one {...} few {...} many {...} other {...}}` with `#` = the locale-formatted number; no `offset`.
- `{x, select, a {...} other {...}}`; `other` is mandatory in plural and select.
- `{n, number}` with the skeletons `::percent`, `::precision-integer`, `::.0`, `::.00` (nothing else: `::.000` and friends are valid FormatJS but outside the contract, and `::integer` is silently ignored by FormatJS 10.7.16, so both are rejected). NaN and infinity print as `Intl` does (`NaN`, `∞`; `ar` and `ru` have their own word for NaN) and select the plural category `other`. A plain `{n}` with a number renders as `String(n)` (not locale-formatted); only `{n, number}` and `#` format.
- Apostrophe quoting exactly as FormatJS: `''` is a literal `'`; `'{'` starts a quoted literal.
- Whitespace between argument tokens is FormatJS's `Pattern_White_Space`: TAB, LF, VT, FF, CR, space, U+0085, U+200E, U+200F, U+2028, U+2029 (NBSP is not). A trailing U+200E/U+200F stays part of the identifier (an unknown argument, as in FormatJS). A `<` starts a tag only before an ASCII letter or `/` (rejected); `<é`, `<日本`, `<1`, `< ` are literal text.
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
| `Time(dt)` / `Date(dt)` | `{"$t":"time"\|"date","ts":"<ISO 8601 with offset>"}`: `ts` is an absolute instant, serialized losslessly with its offset |
| `Duration(seconds)` | `{"$t":"dur","s":300}` |
| `ListOf(items, style="and")` | `{"$t":"list","items":[...],"style":"and"\|"or"}` |
| `Msg(key, **params)` nested | `{"$t":"msg","key":"...","params":{...}}` |
| `Code(key)` | `{"$t":"code","key":"common.mode.cool"}` (an enum rendered through its own catalog key) |

**Time and date semantics.** `i18n.Time`/`i18n.Date` require an aware `datetime`; a naive one is taken as UTC and logged once. Python renders the wall clock of the datetime AS GIVEN, so callers pass Home Assistant local datetimes (`dt_util.as_local(...)`). The dashboard renders per Home Assistant's own per-user setting (`CAI18N.timeZone()`): `hass.locale.time_zone === "server"` (the HA default, also when the setting is absent) → `hass.config.time_zone`; `"local"` → the browser's zone; no `hass` reachable (or no zone known, or an unknown zone id) → the wall clock written in `ts`, i.e. the offset the backend serialized. `CAI18N.timeZone()` returns the IANA id, `'local'`, or `null` for that last case. Times and dates are cross-checked against `Intl.DateTimeFormat` by the conformance fixture (6 timestamps × 16 languages, plus a date per month); ICU's U+202F/U+00A0 are normalised to a plain space on both sides.

**Typed params that cannot be rendered.** `None`/NaN/inf durations, a `Temp` that is not a number, a non-datetime, a malformed wire object and every unknown `$t` render as an em dash on both sides (fixture rows `strict: false`); NaN/inf cannot travel as JSON, so `param_to_wire` sends `{"$t":"bad"}` for them. Python's `Msg.from_wire` never raises outside strict mode.

`Msg.to_wire()` → `{"key", "params"}`; `Msg.from_wire(d)` is the inverse. Temperatures travel as internal Fahrenheit plus the display unit (`"fahrenheit"`/`"celsius"`, the constants in `temperature.py`); the formatter converts. English output of `format_temp(..., lang=None)` is byte-identical to today's.

**English tie rounding.** English temperatures and deltas are the legacy Python f-string (`f"{value:.{d}f}"`): round half to even on the exact binary value (`72.25` → `72.2`, `2.5` → `2`, `1.005` → `1.00`). Every other language (Python and JS) rounds half away from zero on the shortest decimal, as `Intl.NumberFormat` does (`2.5` → `3`, `1.005` → `1.01`). `CAI18N.fmt.temp`/`tempDelta` emulate the English rule exactly for `en`; fixture rows pin both (hand-authored from CPython, cross-checked by an independent BigInt oracle in the generator). Durations use half-even too: 75 and 135 minutes are `1.2h` and `2.2h` in both runtimes.

## Dashboard runtime

`frontend/i18n.js` defines `window.CAI18N`. It needs `frontend/intl-messageformat.iife.js` (FormatJS `intl-messageformat` 10.7.16, BSD-3-Clause, licence in `intl-messageformat.LICENSE.md`; the sourceMappingURL line is stripped; the global is the module namespace, so the code uses `IntlMessageFormat.IntlMessageFormat`). Both load first, before every other script.

| Member | Meaning |
|---|---|
| `init({statusLanguage, base})` | Resolves the language synchronously (so `lang`/`dir` and `<html lang>`/`<html dir>` are right at once; `RTL_LAYOUT_READY` false would keep `<html dir>` `ltr`), then fetches `index.json` and, for `en` and the chosen language, every area file. Returns a Promise that **always resolves** (a failed fetch leaves English/key fallbacks and one console warning); a later call (for example once the status payload is in) re-resolves and retries. |
| `lang`, `locale`, `dir` | Chosen shipped language; `hass.locale` when the parent has it (else `null`); `ltr`/`rtl` (the direction of the language; see `<html dir>` above). |
| `timeZone()` | The zone clock times are shown in: IANA id (`server`, the default), `'local'`, or `null` (render the wall clock written in the wire `ts`). |
| `t(key, params)` | Per-key English fallback; missing everywhere → the key plus one `console.warn`. |
| `tw(wire)` | Renders a `{key, params}` wire object; decodes typed params exactly as Python (proven row by row by the `temp`/`tdelta`/`list`/`dur`/`time`/`date`/`msg`/`code`/bad-parameter/fallback rows of `tests/fixtures/i18n_conformance.json`); a plain string passes through; a wire with no key (`tw({})`) gives `''`. |
| `fmt.number/time/date/dateTime/duration/list/temp/tempDelta` | `Intl.*`-based. `temp`/`tempDelta`: `Intl.NumberFormat(lang, {min/maxFractionDigits: d, useGrouping: false})` of the converted value (Celsius `(f-32)*5/9`, delta `f*5/9`), then `°F`/`°C` with no space. `list`: `Intl.ListFormat` conjunction/disjunction, long. `duration`: the `common.duration.min|hours|hours_frac` messages with the same thresholds as `i18n.format_duration` (minutes = max(1, round-half-even(s/60)); whole hours; else one decimal). `time`/`date`: `Intl.DateTimeFormat`, dashboard-owned. |
| `esc(s)` | The one HTML escaper. |
| `hass()` | The one parent-`hass` lookup (parent, then top window; never throws). |
| `th(key, params)` | Like `t()` but returns safe HTML: the message is escaped and every string param (a formatted temperature, a time, a name) is wrapped in `<bdi>`. Number params stay numbers (plural selection). Only for `innerHTML`. |
| `text(obj, field)` | The one call-site shape for API prose: the `<field>_msg` wire sibling rendered with `tw()` when the backend sent one (and its key is in the catalog), else the plain prose field. `ND.text` is this. |
| `has(key)` | Whether the language or English catalog has the key. |
| `apply(root)` | Fills `[data-i18n="key"]` (`textContent`) and `[data-i18n-attr="title:key;aria-label:key"]` (`setAttribute`); `data-i18n-params='{"n":6}'` passes arguments. Never `innerHTML`. A key with no entry leaves the element's English markup (which a test keeps equal to the catalog). |
| `fmt.hour(d)` | Hour only (`3 PM` / `15`), in `timeZone()` like `fmt.time`. |
| `fmt.intl(opts, lang)` | A zone-aware `Intl.DateTimeFormat` for any options (`timeZone()` plus the 12/24 h setting when `hour` is shown); `lang` is a code or a locales array (`[]` = the browser locale). The one way a page formats an instant it draws itself (chart ticks, readouts). |
| `fmt.dateYear(d)` / `fmt.calendarDay(y, m, d, lang, weekday)` | `date` plus the year when the date is not in the current year (in the display zone); a calendar day given by parts, the same day in any zone. |
| `iso(x)` | A dynamic Latin name isolated (U+2068..U+2069) in right-to-left text; the one helper. |
| `init({areas})` | `areas` limits the fetch to some catalog areas (`entry.html` loads only `dashboard`). |
| `?lang=en-XA` | The pseudo-locale, built on the fly from `en`: every letter accented, ~35% padding, each message bracketed `[...]`; ICU syntax is preserved. Only ever chosen by `?lang=` (never by `hass` or the browser), never listed in `index.json`, formats numbers and dates as English. A visible word that is not bracketed bypassed the catalog (`tests/ui/i18n-dashboard.spec.js`). |

Stage 1 adds the file and the `<script>` tags only; no page text uses `t()` yet, so every page behaves exactly as before. Stage 2 (L9) replaces the duplicated `haAuth`/`esc` helpers in `nd_*.js` with these. Prior art: lboue's closed PR #1562 (French), whose `t()`/`data-i18n`/`?lang=` design this adapts; its French strings are imported with credit in the strings phase.

### P0a results (headless Chromium, library loaded by a page in an iframe, as the panel does)

- `intl-messageformat` 10.7.16 IIFE: 91,494 bytes, sha256 `b200b1cf…c2a2260`, no `export` statement, no console errors.
- All 16 locales render plural, select, number (`21,5` in fr/de/…, `21.5` in en/ja/…) and date.
- A plural with no `other` is rejected at parse (`MISSING_OTHER_CLAUSE`), matching the parity guard.
- Chromium formats `ar` with Latin digits (`1,234`, not Arabic-Indic) and inserts RLM marks in dates; the conformance fixture pins these exact outputs, so the Python renderer matches them. **Decision (N6):** `ar` keeps Latin digits everywhere, because that is what `Intl` and Home Assistant's own frontend produce for `ar` today; switching to Arabic-Indic digits would be one change in both runtimes (the `_NUMBER_SYMBOLS` table and the fixture) if the owner ever wants it.
- A real-browser smoke (Stage 1B): `new_dashboard.html` in an iframe whose parent exposes `hass.language="de"` → `CAI18N.lang === "de"`, no console errors; `"ar"` → `dir === "rtl"`; a missing language file warns once and falls back to English.

## Cache busting

The panel registers `entry.html?v=<hash>`. `_panel_asset_bytes` (`__init__.py`) hashes `entry.html`, `index.html`, `new_dashboard.html`, `i18n.js`, `intl-messageformat.iife.js` and every `*.json` under `frontend/i18n/` in sorted order, so a corrected translation changes the hash. The `StaticPathConfig` registration is unchanged. `tests/test_i18n_panel_wiring.py` pins that a changed or added catalog file changes the hash. Catalogs are loaded once in `async_setup_entry` (`await hass.async_add_executor_job(i18n.load_catalogs)`, skipped when `catalogs_loaded()`).

## Conformance

- `tests/fixtures/i18n_conformance.json`: rows `{"lang", "msg" | "key", "params" (wire), "catalogs"?, "strict"?, "expected"}`. `msg` is an inline pattern (installed as the catalog key `conformance.row`); `key` + `catalogs` renders a key from tiny inline catalogs `{lang: {key: pattern}}` (used for `dur`, nested `msg`/`code` and the fallback rows); `strict: false` marks rows that exercise the never-raise path (Python runs them with strict mode off). Params may hold `{"$f": "nan"|"inf"|"-inf"}` float literals (JSON has no NaN). `expected` is generated by FormatJS + `Intl` in Node (`tools/i18n_conformance_gen.mjs`, which records the Node and ICU versions, reads the language list from `index.json`, and decodes the typed params with its own independent implementation of this spec).
- Python: `tests/test_i18n_conformance.py` (strips U+2068/U+2069 before comparing; a separate test asserts the `ar` wrapping). Node: `tests/i18n_conformance.mjs` loads the real `intl-messageformat.iife.js` and `i18n.js` into a vm sandbox (`tests/i18n_node_shim.mjs`) and renders every row through `CAI18N.tw`. `tests/i18n_runtime.mjs` checks resolution order, code mapping, per-key fallback, typed params, failed fetch and `dir`.
- `tests/test_i18n_plural_drift.py` compares Python's plural table with `Intl.PluralRules` (0..1000 and decimals).
- Times and dates are Python-owned per-language tables, pinned by `tests/test_i18n_format.py` and cross-checked against `Intl.DateTimeFormat` by the fixture's time/date rows (wall clock written in `ts`, see "Time and date semantics").
- CI: `node tests/i18n_runtime.mjs` and `node tests/i18n_conformance.mjs` run in the `lint` job of `validate.yml` (the one the `validate-and-test` gate collects) on Node **22.12.0** (`actions/setup-node@v4`). The expected text embeds one ICU/CLDR release, so `tests/i18n_conformance.mjs` first asserts `process.versions.icu` equals the fixture header's `icu` (76.1) and fails with a clear message instead of hundreds of row diffs. Regenerate with `node tools/i18n_conformance_gen.mjs` (same Node) after reviewing the diff; bumping Node means regenerating and reviewing.

## Design decisions (D1-D9)

- **D1** Two catalogs split by who renders them (HA deletes ICU keys from `translations/`, so HA-rendered text cannot carry ICU). One English source per string. The existing label tables (`NEXT_AUTOMATION_LABELS`, `_MODE_SET_REASON_LABELS`, `OUTDOOR_SENSOR_REASON_LABELS`, `_WRITE_OUTCOME_LABELS`, `SENSOR_REASON_LABELS`, Settings metadata) keep their keys as catalog keys; only their English values move, and their tests are re-pointed at the catalog.
- **D2** Language resolution as above: backend from `hass.config.language`, dashboard per viewer.
- **D3** Messages, not English strings: an ICU subset, vendored FormatJS in JS, a small pure-Python renderer for exactly that subset (no pip requirement), `Msg` with typed params, whole-sentence messages (no code joins rendered sentences), codes for everything code compares (`status_code`, message keys, an explicit Settings `unit`), `{key, params}` siblings in the API.
- **D4** Stored data stays English for records: `event_log` keeps English `reason`/`detail` (via `render_en`) and adds `reason_key`/`reason_params` for display; legacy entries show their English and age out; the briefing stores its `Msg`; investigation reports stay in the language written; `last_update_error` stays English. `outcomes.py` is not touched.
- **D5** One formatter per concept: `temperature.format_temp`/`format_temp_delta` gain optional `lang`; `format_time`/`format_date`/`format_duration` live in `i18n.py` (no HA import); on the dashboard `i18n.js` owns resolution, `t()`, the `Intl` formatters, the one `hass` lookup and one `esc`.
- **D6** Entities and states use HA's own method: `_attr_translation_key`, names and state translations in `translations/`, prose-state sensors render a `Msg` in `hass.config.language` and expose stable `code`/`params` attributes.
- **D7** Workflow: an in-repo tool, no external service (Q5); machine drafts with a glossary and a translation memory; all keys ship translated (Q4); the `en-XA` pseudo-locale tests a branch before translations exist.
- **D8** Layout: physical CSS to logical, mirrored direction arrows, `<bdi>` around numbers/times/names, `overflow-wrap: anywhere` and `hyphens: auto`, `line-break: strict` for CJK.
- **D9** AI: the investigation skill is told to write occupant-facing sections in the language; `## SECTION` headers stay English; AI context uses `render_en`. Issue #1657: the report language is `i18n.hass_language_tag(hass)`, HA's own language code as given (any language HA supports, e.g. `sv`, `pt-BR`, `zh-TW`), not the shipped language from `hass_language`: the model writes it, no catalog is needed. English (any region) adds no instruction. The dashboard labels around the report still follow the shipped languages.

## Owner decisions (2026-11-14)

- **Q1** Portuguese ships as `pt` only; `pt-BR` maps to `pt`.
- **Q2** Classic `index.html` stays English only.
- **Q3** The Debug tab's chrome is translated; field names stay English.
- **Q4** No risk tiers or review gate; every string ships translated.
- **Q5** No Crowdin; an in-repo tool only.
- **Q6** `advises_opening` reads the message key; the 2 goldens that assert English card text are re-derived and re-signed (three-exercise protocol on each).

## Non-goals

hreflang and URL routing (a private panel); logs, brand name and email headers stay English; classic `index.html`.

## How to add a string and the lane workflow

1. Pick the area (`common notify status timeline learning night activity briefing dashboard settings errors ai`); the key is `<area>.<name>`.
2. Write the English value in `frontend/i18n/<area>/en.json` (HA-rendered text: `strings.json` and `translations/en.json`, plain `{name}` only). Use ICU for counts (`plural`) and grammatical variants (`select`); never join rendered sentences in code.
3. Use it: Python `from . import i18n` then `i18n.Msg("<area>.<name>", n=3)`; render with `i18n.render(msg, i18n.hass_language(self.hass))`. Dashboard: `CAI18N.t(...)` or `CAI18N.tw(wire)`. Never hard-code English at a sink; never compare against prose, compare codes.
4. Write the 16 translations in `<area>/<lang>.json` (same keys, same argument names, the language's own plural categories).
5. `python tools/i18n.py lock --area <area> --lang all` records that the translations were made against this English.
6. `python tools/i18n.py check` (parity, HA parity, staleness; `tools/validate.py` runs the same checks). Changing existing English later marks the key stale in every language until it is re-translated and re-locked: `python tools/i18n.py missing`, `stale`.

Summary: write English → 16 translations → `tools/i18n.py lock` → `check`.

## Guards and commands (lane L1)

All check bodies are in `tools/i18n_checks.py`; `tools/validate.py` and `python tools/i18n.py check` both call `run_guards()`, so the pre-commit gate and the CLI cannot disagree. The Python AST finders and the one sink list are in `tests/helpers/ast_walk.py`.

- **Ratchets** (`i18n/baselines/<name>.json`, per-file counts; a file may never gain hits even if another loses some; the test and `check` print `python tools/i18n.py baseline --update` when counts fall; at zero a guard is absolute; `baseline --init` creates, `--update` only lowers):
  - `py_literals`: English literals at Python sinks (`_notify*` title/message, `_attr_name`, `SelectOptionDict(label=)`, `ServiceValidationError`/`HomeAssistantError`, `async_create_issue` placeholders, API `error/message/detail/prose/text/summary/description/title` in `api.py`/`dashboard_api.py`, `_emit_event` display keys, `detail=`, `EVENT_RENDERERS` returns, `strftime`, bare `°F`/`°C` outside `i18n.py`/`temperature.py`/`time_utils.py`). Logs and docstrings are not sinks.
  - `english_matching`: `startswith`/`endswith`/`in`/`==`/regex against English prose, Python and JS. Stored-English readers (`ai_skills_investigator.py`, `log_capture.py`, `tools/sim_harness/outcomes.py`) are exempt by design (D4).
  - `js_dom_literals`: English at DOM sinks (`textContent`/`innerText`/`innerHTML`/`title`/`placeholder`/`alt`/`ariaLabel`, `setAttribute` of title/alt/aria-label/placeholder, `createTextNode`, `insertAdjacentHTML`, Chart.js `label`/`text`) and static HTML text in `frontend/*.js` and `*.html`. The classic `index.html` is excluded (stays English); elements with any `data-i18n*` attribute are covered; in `nd_debug.js` only multi-word prose counts (field names are identifiers).
  - `physical_css`: `margin|padding|border-left/right`, `left:`/`right:`, `text-align`/`float: left|right`, `marginLeft`/`paddingRight` in frontend CSS, HTML and JS style strings (classic `index.html` excluded: never right-to-left).
- **en-XA pseudo-locale** (`i18n.pseudo(text)`, Python side): accents every letter, adds about 35% as a `~` pad, wraps in brackets, keeps ICU syntax. `tests/test_i18n_guards.py` renders EVERY key of both catalogs in en-XA with sampled parameters and fails on an exception, a missing expansion or any ASCII vowel left over (a bypass). Never committed.
- **Translation memory**: `python tools/i18n.py tm-build` rebuilds `i18n/tm.json` (`sha(en) -> {lang: translation}`) from approved translations (lock equals current English); `tm-fill --area A [--lang L] [--dry-run]` fills missing keys whose identical English already has a translation and records them in the lock. Review reused strings that depend on context.
- **Translator prompt**: `python tools/i18n.py prompt --area A --lang L` prints the missing and stale keys with English, previous translation, arguments, length budget, notes (`i18n/notes/<area>.json` and/or `frontend/i18n/<area>/_notes.json`, `{key: note}`), the relevant GLOSSARY rows, the language style rules and its CLDR plural categories.
- **Glossary check**: `python tools/i18n.py glossary [--area A] [--lang L]` lists WARNINGS (never failures; inflection) where an English value contains a term with an approved glossary translation (no `(?)`) that the language's value lacks. `check` and `validate` print only the count.
- **Length budgets**: `i18n/budgets.json` maps `<area>.<key>` (HA keys as `ha:<dotted.key>`) to a maximum number of characters of the longest rendering (plural counts and select variants sampled, text arguments empty), checked in every language including English; failures are errors and a budget for a missing key is an error. Seeded for `common.*`; lanes add theirs (notification titles 60, button labels 24).

## Tests

`tests/test_i18n_panel_wiring.py` (panel hash, status `language`, a broken catalog does not stop setup), `tests/test_panel_entry.py` (panel URL, unchanged), the conformance and drift tests above, `tests/i18n_runtime.mjs`, `tests/i18n_conformance.mjs`. `tests/conftest.py` and `tests_ha/conftest.py` load the catalogs once and switch strict mode on for the whole session (autouse), so no test module can pass while quietly rendering a raw key; a test that needs the production fallbacks turns strict off for itself and the next test starts strict again.

## Briefing

Area `briefing` (`frontend/i18n/briefing/en.json`, 120 keys), lane L8. Code: `briefing.py` (`generate_briefing_msg` builds, `generate_briefing` renders), `nat_vent_plan.py` (`close_timing_msg`, `cutoff_reason_msg`, `reopen_clause_msg` and the English string wrappers `describe_*`), `coordinator.py` (`_briefing_display`, restore/save of `briefing_state`, the send title).

- **One top-level message per day type.** `briefing.full.hot|warm|mild|cool|cold` (and `briefing.full.other` for an unknown day type) place the sections by name: `title`, `tldr`, `body`, `leaving`, `fresh_air`, `grace`, `steps`, `tonight`, `suggestions`. A translator can reorder or drop a section by editing that one message. The short notification text is `briefing.tldr`.
- **Optional parts are select flags, not code joins.** A section or paragraph that may be absent is passed as a named param (`""` when absent) plus a `has_<name>` `yes`/`no` flag (`briefing._slots`), and the message selects on the flag: `{has_grace, select, yes {\n\n{grace}} other {}}`. The catalog, not code, decides where each whole paragraph goes and what separates it. Each day plan is its own message with paragraph slots (`briefing.hot.plan`: opener, close, blinds, reopen, fan; `warm.plan`: main, fan, ac, precool, reopen; `mild.plan`: first, near, tip, close, reopen; `cool.plan`, `cold.plan`, `tonight.plan`). Variants that differ in wording (already-reached vs future close, with or without an evening reopen, heater ran overnight or not) are separate whole-sentence messages, or an inline select inside one sentence when only a clause changes (`{already, select, yes {now} other {at {time}}}`).
- **Each TLDR row is one message** including its label (`briefing.tldr.windows_close_by`, ...). The close time or "now" is the shared `briefing.close_when` message passed as the `when` param; the reason a window closes is the shared `briefing.reason.*` message (`nat_vent_plan.cutoff_reason_msg`), embedded as the `reason` param.
- **Times are `i18n.Time`** with the HA-local aware datetime (the old `_FMT_HOUR` strftime is gone). A bare time of day (bedtime, a static reopen hour) is placed on today's date in the HA zone (`briefing._clock`). Temperatures are `Temp`/`TempDelta`. The armed band `68–74°F` is `briefing.band` (the floor is a bare number in the display unit, the ceiling a `Temp`) because the engine has no range type.
- **A state code inside a sentence is a word, never the code (Issue #1659).** An `hvac_action` or `hvac_mode` / climate state passed into a sentence goes through `hvac_action.hvac_action_word()` / `hvac_mode_word()`, which return `Code("common.hvac_action.<code>")` / `Code("common.hvac_mode_word.<code>")` (the prose form; `common.mode.*` stays the capitalised label). English's word is the code itself, so English text is unchanged; a missing value reads "unknown"; a code HA does not define passes through as its text. Other enumerations follow the same shape with their own key table (`status.pre_cool.outcome.*`, `status.indoor_source.why.*`). On the dashboard, look the code up (`CAI18N.has(key) ? t(key) : code`). `tests/test_i18n_raw_codes_1659.py` renders every affected notification in every language and fails on a raw code.
- **Plain pass-through params.** The operation timeline's step lines (`timeline_surfaces.operations_lines`), the learner's suggestion sentences and the time-of-use hold sentences (`automation.hold_sentence`, `hold_after_text`) are other lanes' text. They enter as string (or `Msg`) params and are not re-translated here; when those lanes return a `Msg` the same params carry it.
- **Strings that stay English by contract.** `nat_vent_plan.describe_close_timing`, `describe_reopen_clause`, `describe_nat_vent_cutoff_reason` and `briefing._band_text`/`_trend_description` return English (`render_en`) for existing callers and the AI context; occupant-language callers use the `*_msg` functions.
- **Persisted briefing.** `generate_briefing` returns `BriefingText`, a `str` (the English text, so `coordinator._last_briefing` and `_last_briefing_short` read as before) that also carries `.msg`. `briefing_state` stores `last_text`/`last_text_short` (English, unchanged) plus `last_msg`/`last_msg_short` (the `Msg.to_wire()` dicts). On restore the message is rebuilt and the text re-renders in the current language (`BriefingText.rendered(lang)`, cached per language); a state saved before #1341 has no message and shows its stored English; a malformed message is dropped with one warning. Notifications, email and the status `briefing`/`briefing_short` fields render in `hass.config.language`; the status payload adds `briefing_msg`/`briefing_short_msg` siblings.
- **Length.** The push notification carries the TLDR (`briefing.tldr`); keep each translated language's rendered TLDR within the notification budget (see the report for per-language entries).
