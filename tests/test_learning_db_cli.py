"""Tests for the --daily CLI flag in tools/learning_db.py (Part 3, TDD red phase).

Tests target `_print_daily_records(db, n=30)` and the `--daily` argparse flag.
`_print_daily_records` does NOT exist yet — all tests here should fail with
AttributeError (not ImportError) until Part 3 is implemented.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Import tools/learning_db.py without triggering SSH or .env side-effects.
# We load it via importlib so the module-level sys.path insert in the tool
# file runs cleanly, but we never call main() or fetch_learning_db().
# ---------------------------------------------------------------------------

_TOOLS_DIR = Path(__file__).parent.parent / "tools"


def _load_learning_db_module():
    """Load tools/learning_db.py as a module, isolated from SSH calls."""
    spec = importlib.util.spec_from_file_location("learning_db", _TOOLS_DIR / "learning_db.py")
    mod = importlib.util.module_from_spec(spec)
    # Insert tools/ into sys.path the same way the module does, so its own
    # `from ha_logs import ...` doesn't blow up.
    if str(_TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOLS_DIR))
    spec.loader.exec_module(mod)
    return mod


# Load once at module level — import errors surface immediately.
_ldb = _load_learning_db_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(records: list[dict]) -> dict:
    """Return a minimal learning DB dict containing the given records list."""
    return {"records": records}


def _make_record(**kwargs) -> dict:
    """Return a minimal DailyRecord-shaped dict with sensible defaults.

    Only the fields exercised by _print_daily_records need to be present;
    missing new fields (setback_*) are intentionally absent to test graceful
    handling of old records.
    """
    base = {
        "date": "2026-05-17",
        "day_type": "cool",
        "hvac_mode_recommended": "heat",
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Tests for _print_daily_records
# ---------------------------------------------------------------------------


class TestPrintDailyRecords:
    """Unit tests for the (not-yet-implemented) _print_daily_records function."""

    def _call(self, db: dict, n: int = 30) -> None:
        """Call _print_daily_records (output captured by capsys in each test)."""
        _ldb._print_daily_records(db, n=n)

    def test_empty_records_prints_header_and_no_crash(self, capsys):
        """Empty db['records'] must print a header and a 'no records' notice."""
        db = _make_db([])
        _ldb._print_daily_records(db, n=30)
        out = capsys.readouterr().out
        # Header must be present
        assert "Date" in out
        # Some indication there are no records
        assert "no record" in out.lower() or "(no" in out.lower() or out.strip().count("\n") >= 1

    def test_skipped_reason_hvac_off_in_output(self, capsys):
        """A record with setback_skipped_reason='hvac_off' must show 'hvac_off' in output."""
        record = _make_record(
            date="2026-05-17",
            day_type="warm",
            hvac_mode_recommended="off",
            setback_skipped_reason="hvac_off",
        )
        db = _make_db([record])
        _ldb._print_daily_records(db, n=30)
        out = capsys.readouterr().out
        assert "hvac_off" in out

    def test_setback_heat_applied_and_depth_in_output(self, capsys):
        """A record with setback_heat_applied_f=60.0 and setback_depth_f=8.0 must show both."""
        record = _make_record(
            date="2026-05-09",
            day_type="cool",
            hvac_mode_recommended="heat",
            setback_heat_applied_f=60.0,
            setback_depth_f=8.0,
            setback_was_adaptive=True,
        )
        db = _make_db([record])
        _ldb._print_daily_records(db, n=30)
        out = capsys.readouterr().out
        assert "60.0" in out
        assert "8.0" in out

    def test_setback_was_adaptive_true_shows_yes(self, capsys):
        """A record with setback_was_adaptive=True must show 'yes' (or 'True') in output."""
        record = _make_record(
            date="2026-05-09",
            day_type="cool",
            hvac_mode_recommended="heat",
            setback_heat_applied_f=60.0,
            setback_depth_f=8.0,
            setback_was_adaptive=True,
        )
        db = _make_db([record])
        _ldb._print_daily_records(db, n=30)
        out = capsys.readouterr().out
        assert "yes" in out.lower() or "true" in out.lower()

    def test_n_limits_rows_to_last_n(self, capsys):
        """With 35 records and n=30, only 30 rows should be printed."""
        records = [
            _make_record(date=f"2026-04-{i:02d}", day_type="cool")
            for i in range(1, 36)  # 35 records
        ]
        db = _make_db(records)
        _ldb._print_daily_records(db, n=30)
        out = capsys.readouterr().out
        # Each data row will contain a date string; the first 5 records should
        # be absent (only the last 30 are shown).
        # The first 5 dates are 2026-04-01 through 2026-04-05.
        assert "2026-04-01" not in out
        assert "2026-04-05" not in out
        # The last record (2026-04-35 wraps, but day 35 doesn't exist — we used
        # padded numbers, so 2026-04-35 won't parse, but the string itself will
        # appear in the output for records 6–35 which are shown).
        # Simpler: count lines that look like date rows (start with "2026-").
        data_lines = [ln for ln in out.splitlines() if ln.strip().startswith("2026-")]
        assert len(data_lines) == 30, f"Expected 30 data rows, got {len(data_lines)}. Output:\n{out}"


# ---------------------------------------------------------------------------
# Tests for --daily argparse flag
# ---------------------------------------------------------------------------


class TestDailyArgparseFlag:
    """Verify the --daily argparse flag is wired up correctly in main()'s parser.

    We reach into the module to extract the parser without calling main() (which
    would trigger SSH). If the flag doesn't exist yet, parse_args() raises SystemExit
    or the attribute is missing — both are acceptable red-phase failures.
    """

    def _get_parser(self) -> argparse.ArgumentParser:
        """Reconstruct the parser as main() would build it.

        Since main() is not easily decomposed yet, we build a minimal parser
        that mirrors what main() is expected to have after Part 3 is implemented,
        then parse against the module's actual parser by calling parse_known_args
        on a fresh invocation of the parser-builder helper.

        Strategy: parse sys.argv-style args through the module's argparse setup
        by temporarily patching sys.argv and calling the private parser-builder
        if one is extracted, OR by inspecting main() source to find the parser.

        Simpler approach: import the module and call parse_args directly via a
        helper that mirrors main()'s parser construction. If --daily is not
        registered, argparse raises SystemExit(2).
        """
        # We re-exec just the argparse block by extracting it from main().
        # Easier: build our own parser with the expected flags and assert the
        # module's main() would accept the same args without error.
        #
        # Real test: run the module's own parser directly.
        # main() builds `parser` locally — we can't reach it without calling main().
        # So we test via subprocess OR by making _build_parser() a module-level
        # function (which is what the implementation should do).
        #
        # For the TDD red phase, we test the expected interface:
        # `_ldb._build_parser()` should exist and return an ArgumentParser.
        parser = _ldb._build_parser()
        return parser

    def test_daily_flag_n_5(self):
        """--daily 5 should parse to daily_n=5 (or equivalent) with show_daily truthy."""
        parser = self._get_parser()
        args = parser.parse_args(["--daily", "5"])
        # The implementation may use args.daily_n, args.daily, etc.
        # We accept any attribute that holds the integer 5 when --daily 5 is passed.
        daily_n = getattr(args, "daily_n", None) or getattr(args, "daily", None)
        assert daily_n == 5, f"Expected daily_n=5, got args={vars(args)}"

    def test_daily_flag_default_n(self):
        """--daily with no N argument should default to 30."""
        parser = self._get_parser()
        # nargs='?' lets --daily be used with or without a value.
        args = parser.parse_args(["--daily"])
        daily_n = getattr(args, "daily_n", None) or getattr(args, "daily", None)
        assert daily_n == 30, f"Expected default daily_n=30, got args={vars(args)}"

    def test_daily_flag_absent_means_no_show(self):
        """When --daily is not passed, the daily section should not be requested."""
        parser = self._get_parser()
        args = parser.parse_args([])
        # Either daily_n is None/0 or a show_daily flag is False.
        daily_n = getattr(args, "daily_n", None) or getattr(args, "daily", None)
        show_daily = getattr(args, "show_daily", None)
        # At least one of these must indicate "not requested".
        not_requested = (daily_n is None) or (show_daily is False) or (show_daily is None)
        assert not_requested, f"Expected daily not requested when flag absent, got args={vars(args)}"
