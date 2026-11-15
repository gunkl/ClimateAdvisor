"""Translation guards beyond parity (Issue #1341, lane L1): ratchets, finders, en-XA, translation memory, prompt,
glossary and length budgets. Every finder has a negative control (it must be able to fail)."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

from tests.helpers import ast_walk
from tests.test_i18n_checks import tree  # noqa: F401  (fixture: throwaway copy of catalogs, locks, translations)
from tools import i18n as cli

checks = cli.checks
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))


def _sinks(src: str, rel: str = "x.py") -> list[ast_walk.Hit]:
    return ast_walk.find_literals_at_sinks(ast.parse(src), rel)


def _compares(src: str, rel: str = "x.py") -> list[ast_walk.Hit]:
    return ast_walk.find_prose_comparisons(ast.parse(src), rel)


# ------------------------------------------------------------------------------------------------ the committed repo
def test_committed_repository_passes_every_guard():
    results = checks.run_guards()
    assert {k: v for k, v in results.items() if v} == {}
    assert set(results) == {
        "parity",
        "ha parity",
        "stale",
        "length budgets",
        "ratchet py_literals",
        "ratchet english_matching",
        "ratchet js_dom_literals",
        "ratchet physical_css",
    }


def test_validate_hook_runs_the_same_guards():
    from tools import validate

    result = validate.check_i18n(None)
    assert result.passed, result.errors


def test_cli_check_prints_every_guard(capsys):
    assert cli.main(["check"]) == 0
    out = capsys.readouterr().out
    for name in ("length budgets", "ratchet py_literals", "ratchet physical_css", "glossary:"):
        assert name in out


def test_every_ratchet_has_a_committed_baseline_that_matches_a_real_count():
    for name in checks.RATCHETS:
        baseline = checks.load_baseline(name)
        assert baseline is not None, name
        payload = json.loads(checks.baseline_path(name).read_text(encoding="utf-8"))
        assert payload["total"] == sum(baseline.values())


# ------------------------------------------------------------------------------------------------ python sink finder
@pytest.mark.parametrize(
    ("src", "sink"),
    [
        ("self._notify('Heating was turned off', 'Climate Advisor update', 'x')", "_notify"),
        (
            "self._notify_in_background(title='Window left open', message=f'Closed {n} windows', kind='x')",
            "_notify_in_background",
        ),
        ("class S:\n    _attr_name = 'Indoor temperature'", "_attr_name"),
        ("SelectOptionDict(value='a', label='Dedicated sensor')", "SelectOptionDict(label=)"),
        ("raise ServiceValidationError('Unknown zone entry')", "ServiceValidationError"),
        ("raise HomeAssistantError(message='Could not reach the thermostat')", "HomeAssistantError(message=)"),
        (
            "ir.async_create_issue(h, d, i, translation_placeholders={'reason': 'The sensor is offline'})",
            "async_create_issue.placeholder[reason]",
        ),
        ("self._emit_event('x', {'summary': 'Fan stopped early'})", "_emit_event[summary]"),
        ("Violation(detail='The fan ran too long today')", "detail="),
        ("x = dt.strftime('%H:%M')", "strftime"),
        ("label = f'{t}°F'", "unit-symbol"),
    ],
)
def test_sink_finder_flags_english(src, sink):
    assert {h.sink.split("(")[0] for h in _sinks(src)} == {sink.split("(")[0]}


def test_sink_finder_flags_api_fields_only_in_api_modules():
    src = "def f():\n    return {'error': 'Not available right now', 'code': 'not_available'}\n"
    assert [h.sink for h in _sinks(src, "api.py")] == ["api[error]"]
    assert _sinks(src, "coordinator.py") == []


def test_sink_finder_flags_event_renderer_return_values():
    src = "def _r(payload, unit):\n    return 'Fan stopped', 'Settings were restored'\nEVENT_RENDERERS = {'a': _r}\n"
    assert {h.sink for h in _sinks(src)} == {"EVENT_RENDERERS._r"}
    assert len(_sinks(src)) == 2


@pytest.mark.parametrize(
    "src",
    [
        "self._notify(i18n.render(msg, lang), 'x', 'y')",  # already a message
        "self._notify(title, message, 'type')",
        "class S:\n    _attr_name = None",
        "SelectOptionDict(value='a', label=lb)",
        "raise ServiceValidationError(translation_domain=DOMAIN, translation_key='unknown_zone')",
        "Violation(detail='nat_vent_fsm_authoritative')",  # an identifier, not prose
        "self._emit_event('x', {'reason': 'Fan stopped early because of wind'})",  # stored English records
        "_LOGGER.warning('Could not reach the thermostat at 68°F')",  # logs stay English
        "ir.async_create_issue(h, d, i, translation_placeholders={'name': name})",
        "x = {'error': 'Not available right now'}",  # not an API module
        "def f():\n    '''Docstring that mentions 68°F in prose.'''",
        "x = dt.isoformat()",
    ],
)
def test_sink_finder_ignores_codes_messages_logs_and_identifiers(src):
    assert _sinks(src) == []


def test_formatters_may_use_units_and_strftime():
    assert _sinks("x = dt.strftime('%H')\ny = '°F'", "temperature.py") == []


def test_is_english_prose_heuristic():
    prose = ast_walk.is_english_prose
    assert prose("The fan is on") and prose("Heat", min_words=1) and not prose("Heat")
    assert not prose("climate_advisor.fan_on") and not prose("sensor.indoor_temp") and not prose("°F")
    assert not prose("Climate Advisor") and not prose("{name} {n}") and not prose("heat", min_words=1)


# ------------------------------------------------------------------------------------------------ prose matching finder
@pytest.mark.parametrize(
    "src",
    [
        "if text.startswith('Opening windows'):\n    pass",
        "if text.endswith(' (deferred retry)'):\n    pass",
        "if 'not applied' in reason:\n    pass",
        "if state == 'running (untracked)':\n    pass",
        "re.search(r'remote timer: (\\d+)h', text)",
    ],
)
def test_prose_comparison_finder_flags_english(src):
    assert len(_compares(src)) == 1


@pytest.mark.parametrize(
    "src",
    [
        "if text.startswith('sensor.'):\n    pass",
        "if mode == 'heat_cool':\n    pass",
        "if 'a' == 'b c':\n    pass",
        "if reason_code == REASON_NOT_APPLIED:\n    pass",
        "re.compile(r'^[a-z_]+$')",
    ],
)
def test_prose_comparison_finder_ignores_codes(src):
    assert _compares(src) == []


def test_stored_english_readers_are_exempt_by_design():
    src = "if text.startswith('Opening windows'):\n    pass"
    assert _compares(src, "ai_skills_investigator.py") == []
    assert "ai_skills_investigator.py" in ast_walk.STORED_ENGLISH_READERS


# ------------------------------------------------------------------------------------------------ frontend finders
def _frontend(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_js_dom_finder_flags_english_at_dom_sinks(tmp_path):
    js = (
        "el.textContent = 'The briefing could not be loaded';\n"
        'el.innerHTML = `<div class="empty">No suggestions right now</div>`;\n'
        "btn.setAttribute('aria-label', 'Scroll back');\n"
        'input.placeholder = "Type a name";\n'
        "datasets.push({ label: 'Windows open' });\n"
    )
    hits = checks.find_js_dom_english(_frontend(tmp_path, {"nd_x.js": js}))
    assert [h.sink for h in hits] == [
        "textContent",
        "innerHTML",
        "setAttribute(aria-label)",
        "placeholder",
        "chart.label",
    ]


def test_js_dom_finder_ignores_catalog_calls_comments_ids_and_vendor(tmp_path):
    js = (
        "el.textContent = t('status.loading');\n"
        "el.textContent = '';\n"
        "el.className = 'some-class';\n"
        "// el.textContent = 'A commented out sentence';\n"
        "el.title = unit + '°F';\n"
        "el.textContent = value_with_underscore;\n"
        'el.innerHTML = `<span class="x">${n}</span>`;\n'
    )
    files = {
        "nd_ok.js": js,
        "hammer.min.js": "el.textContent = 'Vendor text goes here';",
        "index.html": "<p>Classic stays English</p>",
    }
    assert checks.find_js_dom_english(_frontend(tmp_path, files)) == []


def test_html_finder_flags_static_text_and_attributes_unless_marked(tmp_path):
    page = (
        "<html><body><h1>Dashboard overview</h1><button aria-label='Scroll back'>x</button>"
        "<p data-i18n='a.b'>Marked text</p><button title='Zoom in' data-i18n-title='a.c'>z</button>"
        "<script>var a = 'not text';</script><style>.a { margin: 0 }</style></body></html>"
    )
    hits = checks.find_js_dom_english(_frontend(tmp_path, {"p.html": page}))
    assert sorted(h.sink for h in hits) == ["html.aria-label", "html.text"]


def test_nd_debug_field_names_do_not_count_but_prose_does(tmp_path):
    js = "a.textContent = 'Indoor'; b.textContent = 'Not enough data yet';"
    assert [h.text for h in checks.find_js_dom_english(_frontend(tmp_path, {"nd_debug.js": js}))] == [
        "Not enough data yet"
    ]


def test_js_prose_comparison_finder(tmp_path):
    js = "if (s.startsWith('Open windows')) {}\nif (x === 'not running now') {}\nif (k === 'heat_cool') {}\n"
    hits = checks.find_js_prose_comparisons(_frontend(tmp_path, {"nd_c.js": js}))
    assert [h.text for h in hits] == ["Open windows", "not running now"]


def test_physical_css_finder_flags_physical_and_allows_logical(tmp_path):
    css = (
        ".a { margin-left: 4px; padding-right: 2px; text-align: right; float: left; left: 0; border-left: 1px solid }\n"
    )
    js = "el.style.cssText = 'margin-right:4px'; el.style.marginLeft = '2px';"
    good = ".b { margin-inline-start: 4px; padding-inline-end: 2px; text-align: start; inset-inline-start: 0 }\n"
    hits = checks.find_physical_css(_frontend(tmp_path, {"a.css": css, "b.css": good, "c.js": js, "index.html": css}))
    assert sorted({h.file for h in hits}) == ["frontend/a.css", "frontend/c.js"]
    assert len([h for h in hits if h.file == "frontend/a.css"]) == 6
    assert len([h for h in hits if h.file == "frontend/c.js"]) == 2


# ------------------------------------------------------------------------------------------------ ratchet logic
def _hits(**per_file: int) -> list[ast_walk.Hit]:
    return [ast_walk.Hit(f, i + 1, "s", "Some english") for f, n in per_file.items() for i in range(n)]


def test_ratchet_passes_at_and_below_the_baseline():
    assert checks.baseline_ratchet("t", lambda: _hits(a=2, b=1), {"a": 2, "b": 1}).errors == []
    assert checks.baseline_ratchet("t", lambda: _hits(a=1), {"a": 2, "b": 1}).errors == []


def test_ratchet_fails_on_a_new_hit_in_a_known_or_new_file():
    assert checks.baseline_ratchet("t", lambda: _hits(a=3), {"a": 2}).errors
    assert checks.baseline_ratchet("t", lambda: _hits(a=2, c=1), {"a": 2}).errors


def test_ratchet_is_per_file_so_one_file_cannot_pay_for_another():
    result = checks.baseline_ratchet("t", lambda: _hits(a=0, b=3), {"a": 3, "b": 2})
    assert len(result.errors) == 1 and result.errors[0].startswith("b:")


def test_ratchet_tells_how_to_lower_the_baseline():
    result = checks.baseline_ratchet("t", lambda: _hits(a=1), {"a": 3})
    assert result.errors == []
    assert any("baseline --update" in note for note in result.notes)


def test_ratchet_with_zero_hits_is_absolute():
    result = checks.baseline_ratchet("t", lambda: [], {})
    assert any("absolute" in note for note in result.notes)
    assert checks.baseline_ratchet("t", lambda: _hits(a=1), {}).errors


def test_missing_baseline_is_an_error(tmp_path):
    result = checks.baseline_ratchet("nope", lambda: [], baselines_dir=tmp_path)
    assert result.errors and "baseline --init" in result.errors[0]


def test_baseline_update_only_lowers(tmp_path, monkeypatch):
    # the repository's own physical-CSS count is 0 (#1341 PR C), so the finder is replaced by two synthetic hits
    hits = [
        ast_walk.Hit("frontend/chart_v2.css", 1, "css", "margin-left:"),
        ast_walk.Hit("frontend/new.css", 2, "css", "left:"),
    ]
    monkeypatch.setitem(checks.RATCHETS, "physical_css", (lambda: hits, "synthetic"))
    path = checks.baseline_path("physical_css", tmp_path)
    path.write_text(json.dumps({"files": {"frontend/chart_v2.css": 1000, "gone.css": 5}}), encoding="utf-8")
    counts = checks.write_baseline("physical_css", init=False, baselines_dir=tmp_path)
    assert counts["frontend/chart_v2.css"] == 1
    assert "gone.css" not in counts
    # a file that is not in the old baseline cannot enter through --update
    assert all(f in {"frontend/chart_v2.css"} for f in counts)
    with pytest.raises(FileExistsError):
        checks.write_baseline("physical_css", init=True, baselines_dir=tmp_path)


def test_baseline_cli_needs_exactly_one_mode():
    with pytest.raises(SystemExit):
        cli.main(["baseline"])


# ------------------------------------------------------------------------------------------------ en-XA
def test_pseudo_accents_expands_and_brackets():
    eng = checks.engine()
    out = eng.pseudo("Heating is on")
    assert out.startswith("[") and out.endswith("]") and "~" in out
    assert not re.search("[aeiouAEIOU]", out)
    assert len(out) >= len("Heating is on") * 1.35


def test_pseudo_preserves_icu_syntax():
    eng = checks.engine()
    src = "{n, plural, one {# min left for {who}} other {# mins left}} '{literal}' {t, number, ::percent}"
    out = eng.pseudo(src)
    assert "{n, plural, one {# " in out and "{who}" in out and "other {# " in out
    assert "'{literal}'" in out and "{t, number, ::percent}" in out
    assert eng.format_message(out, "en", {"n": 1, "who": "Sam", "t": 0.5}).startswith("[")


def test_pseudo_of_a_message_without_letters_is_only_bracketed():
    assert checks.engine().pseudo("{n}") == "[{n}]"


def test_every_catalog_key_renders_in_en_xa_with_expansion():
    """Proves each key renders without error from en, and that every literal passed through ``pseudo``."""
    eng = checks.engine()
    catalogs = checks.catalogs_by_area()
    rendered = 0
    for area, langs in catalogs.items():
        for key, text in langs["en"].items():
            if area == checks.HA_AREA:
                names = checks._placeholders(text)  # noqa: SLF001
                pairs = [
                    (
                        eng.pseudo(text).format(**{n: "Sample" for n in names}),
                        text.format(**{n: "Sample" for n in names}),
                        {},
                    )
                ]
            else:
                pseudo = eng.pseudo(text)
                pairs = [
                    (eng.format_message(pseudo, "en", p), eng.format_message(text, "en", p), p)
                    for p in checks.sample_param_sets(text)
                ]
            for out, plain, params in pairs:
                where = f"{area}: {key}"
                assert out.startswith("[") and out.endswith("]"), where
                if re.search("[A-Za-z]", plain.replace("Sample", "")):
                    assert "~" in out and len(out) > len(plain), where
                leftover = out.replace("Sample", "")
                for value in params.values():  # a sampled select value echoed by `other {{x}}` is data, not text
                    if isinstance(value, str) and value:
                        leftover = leftover.replace(value, "")
                assert not re.search("[aeiouAEIOU]", leftover), f"{where}: unaccented English in {out!r}"
                rendered += 1
    assert rendered >= 100


# ------------------------------------------------------------------------------------------------ translation memory
@pytest.fixture
def memory(tree, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(checks, "TM_PATH", tmp_path / "tm.json")
    monkeypatch.setattr(checks, "NOTES_DIR", tmp_path / "notes")
    for path in tree[2].glob("*.json"):  # memory tests reason about the `common` area only
        if path.stem != "en":
            path.unlink()
    return tree


def _add_key(i18n_dir: Path, area: str, lang: str, key: str, text: str) -> None:
    path = i18n_dir / area / f"{lang}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[key] = text
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_tm_build_maps_hash_of_english_to_approved_translations(memory):
    tm = cli.build_tm()
    sha = checks.en_hash("Unknown")
    assert set(tm[sha]) == {c for c in checks.engine().LANGUAGES if c != "en"}
    ja = json.loads((memory[0] / "common" / "ja.json").read_text(encoding="utf-8"))
    assert tm[sha]["ja"] == ja["common.unknown"]


def test_tm_build_ignores_unapproved_translations(memory):
    i18n_dir, locks, _trans = memory
    lock = json.loads((locks / "common.json").read_text(encoding="utf-8"))
    lock["ja"]["common.unknown"] = "000000000000"  # English changed after ja was written
    (locks / "common.json").write_text(json.dumps(lock), encoding="utf-8")
    assert "ja" not in cli.build_tm()[checks.en_hash("Unknown")]


def test_tm_fill_reuses_identical_english_and_locks_it(memory, capsys):
    i18n_dir, locks, _trans = memory
    cli._dump(checks.TM_PATH, cli.build_tm())  # noqa: SLF001
    _add_key(i18n_dir, "common", "en", "common.unknown_again", "Unknown")
    _add_key(i18n_dir, "common", "en", "common.brand_new", "A sentence nobody has translated yet")
    assert cli.main(["tm-fill", "--area", "common"]) == 0
    assert "common.unknown_again" in capsys.readouterr().out
    ja = json.loads((i18n_dir / "common" / "ja.json").read_text(encoding="utf-8"))
    assert ja["common.unknown_again"] == ja["common.unknown"]
    assert "common.brand_new" not in ja  # no memory entry: stays missing
    assert not [e for e in checks.check_parity(i18n_dir) if "unknown_again" in e]
    assert not [e for e in checks.check_stale(locks, i18n_dir) if "unknown_again" in e]
    missing = checks.missing_keys(checks.catalogs_by_area(i18n_dir), "common", "ja")
    assert [k for _a, _l, k in missing] == ["common.brand_new"]


def test_tm_fill_dry_run_writes_nothing(memory):
    i18n_dir = memory[0]
    cli._dump(checks.TM_PATH, cli.build_tm())  # noqa: SLF001
    _add_key(i18n_dir, "common", "en", "common.unknown_again", "Unknown")
    before = (i18n_dir / "common" / "ja.json").read_text(encoding="utf-8")
    assert cli.tm_fill("common", "ja", dry_run=True) == [("common", "ja", "common.unknown_again")]
    assert (i18n_dir / "common" / "ja.json").read_text(encoding="utf-8") == before


def test_tm_fill_requires_an_area():
    with pytest.raises(SystemExit):
        cli.main(["tm-fill"])


# ------------------------------------------------------------------------------------------------ translator prompt
def test_prompt_lists_missing_and_stale_keys_with_notes_glossary_and_rules(memory):
    i18n_dir, locks, _trans = memory
    _add_key(i18n_dir, "common", "en", "common.new_mode", "Heat is on in {room}")
    en = json.loads((i18n_dir / "common" / "en.json").read_text(encoding="utf-8"))
    en["common.mode.off"] = "Switched off"  # changes the English: ja/fr are now stale for it
    (i18n_dir / "common" / "en.json").write_text(json.dumps(en, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    (i18n_dir / "common" / "_notes.json").write_text(
        json.dumps({"common.new_mode": "room is a room name"}), encoding="utf-8"
    )
    (checks.NOTES_DIR).mkdir()
    (checks.NOTES_DIR / "common.json").write_text(json.dumps({"common.mode.off": "button label"}), encoding="utf-8")
    text = cli.build_prompt("common", "fr")
    assert "### common.new_mode" in text and "English: Heat is on in {room}" in text
    assert "Translator note: room is a room name" in text  # <area>/_notes.json
    assert "### common.mode.off" in text and "Translator note: button label" in text  # i18n/notes/<area>.json
    assert "Previous translation (English changed)" in text
    assert "Chauffage" in text and "vous" in text  # glossary row and style rule for fr
    assert "one, many, other" in text  # CLDR cardinal categories of fr
    assert "Maximum length: 24" in text  # budget
    assert "Arguments: room:arg" in text
    assert cli.main(["prompt", "--area", "common", "--lang", "fr"]) == 0


def test_prompt_for_the_ha_catalog_forbids_icu(memory):
    text = cli.build_prompt("ha", "de")
    assert "plain `{name}` placeholders ONLY" in text
    assert "### config." in text or "### options." in text


def test_prompt_needs_area_and_language():
    with pytest.raises(SystemExit):
        cli.main(["prompt", "--area", "common"])


# ------------------------------------------------------------------------------------------------ glossary
def test_glossary_parses_terms_styles_and_approval():
    glossary = checks.parse_glossary()
    assert set(glossary["style"]) == set(checks.engine().LANGUAGES) - {"en"}
    heat = next(t for t in glossary["terms"] if t["en"] == "Heat")
    assert heat["approved"]["fr"] == "Chauffage" and "en" not in heat["approved"]
    unsure = next(t for t in glossary["terms"] if t["en"] == "economizer")
    assert "fr" not in unsure["approved"]  # carries (?)
    thermostat = next(t for t in glossary["terms"] if t["en"] == "thermostat")
    assert thermostat["approved"]["fr"] == "thermostat"  # gender mark stripped


def test_glossary_warns_when_the_approved_term_is_missing_and_never_fails():
    glossary = {"terms": [{"en": "thermostat", "approved": {"fr": "thermostat", "de": "Thermostat"}}]}
    catalogs = {
        "notify": {
            "en": {"notify.a": "The thermostat was changed"},
            "fr": {"notify.a": "Le chauffage a changé"},
            "de": {"notify.a": "Das Thermostat wurde geändert"},
        }
    }
    rows = checks.glossary_warnings(catalogs, glossary)
    assert rows == ["notify/fr: notify.a: expected glossary term 'thermostat' for 'thermostat'"]


def test_glossary_command_exits_zero(capsys):
    assert cli.main(["glossary"]) == 0
    assert "glossary warnings" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------------ length budgets
def test_budget_failure_is_an_error_in_every_language(memory, tmp_path):
    i18n_dir, _locks, trans = memory
    budgets = tmp_path / "budgets.json"
    budgets.write_text(json.dumps({"common.mode.fan_only": 3}), encoding="utf-8")
    errors = checks.check_budgets(budgets, i18n_dir, trans)
    assert any(e.startswith("common/en:") for e in errors) and any(e.startswith("common/de:") for e in errors)


def test_budget_measures_the_longest_plural_rendering(memory, tmp_path):
    i18n_dir, _locks, trans = memory
    budgets = tmp_path / "budgets.json"
    budgets.write_text(json.dumps({"common.duration.min": 6}), encoding="utf-8")
    assert checks.check_budgets(budgets, i18n_dir, trans)  # "100 min" is 7 chars
    budgets.write_text(json.dumps({"common.duration.min": 24}), encoding="utf-8")
    assert checks.check_budgets(budgets, i18n_dir, trans) == []


def test_budget_for_an_unknown_key_is_an_error(memory, tmp_path):
    i18n_dir, _locks, trans = memory
    budgets = tmp_path / "budgets.json"
    budgets.write_text(json.dumps({"common.no_such_key": 10}), encoding="utf-8")
    assert checks.check_budgets(budgets, i18n_dir, trans) == [
        "budgets: common.no_such_key is not an English catalog key (remove or fix it)"
    ]


def test_committed_budgets_cover_common_keys():
    # Lanes add budgets for their own keys (#1341 PR B: briefing.title/tldr); every budget must name a real English
    # key, and the seeded common.* keys stay covered.
    budgets = checks.load_budgets()
    english = {k for langs in checks.catalogs_by_area().values() for k in langs.get("en", {})}
    assert budgets and all(k in english for k in budgets)
    assert any(k.startswith("common.") for k in budgets)


def test_script_view_closes_on_any_script_end_tag_form():
    """CodeQL py/bad-tag-filter: ``</script >`` and ``</SCRIPT foo>`` end an inline script too."""
    from pathlib import Path

    html = '<p>x</p><script>var a = "in";</script ><p>after</p><SCRIPT>var b = 1;</SCRIPT x>'
    view = checks._script_view(Path("page.html"), html)  # noqa: SLF001
    assert '"in"' in view and "var b" in view
    assert "after" not in view
