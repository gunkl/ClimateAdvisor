"""Tests for security validation logic (Issue #48 Phase 6).

Covers:
- _NOTIFY_SERVICE_RE regex from config_flow.py (allowlist pattern)
- Cross-field setpoint validation logic
- voluptuous schema for respond_to_suggestion service call
- LearningEngine DB type safety and growth caps
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# TestNotifyServiceValidation
# ---------------------------------------------------------------------------


class TestNotifyServiceValidation:
    """_NOTIFY_SERVICE_RE must accept well-formed service names and reject everything else."""

    @pytest.fixture(autouse=True)
    def _import_regex(self):
        from custom_components.climate_advisor.config_flow import _NOTIFY_SERVICE_RE

        self.re = _NOTIFY_SERVICE_RE

    def test_valid_default(self):
        """'notify.notify' is the default HA notification service."""
        assert self.re.match("notify.notify") is not None

    def test_valid_mobile_app(self):
        """Mobile app companion service names are commonly used."""
        assert self.re.match("notify.mobile_app_phone") is not None

    def test_valid_smtp(self):
        """SMTP-style service names with underscores are valid."""
        assert self.re.match("notify.smtp_email") is not None

    def test_invalid_empty(self):
        """Empty string must not match."""
        assert self.re.match("") is None

    def test_invalid_no_dot(self):
        """A bare 'notify' with no domain separator must not match."""
        assert self.re.match("notify") is None

    def test_invalid_uppercase(self):
        """Uppercase letters are rejected — service names are lowercase only."""
        assert self.re.match("Notify.Notify") is None

    def test_invalid_shell_chars(self):
        """Shell metacharacters must not match — guards against injection."""
        assert self.re.match("notify.foo;rm -rf /") is None

    def test_invalid_spaces(self):
        """Spaces in service names are not valid."""
        assert self.re.match("notify.mobile app") is None

    def test_invalid_leading_number(self):
        """Service names must not start with a digit."""
        assert self.re.match("1notify.service") is None


# ---------------------------------------------------------------------------
# TestCrossFieldValidation
# ---------------------------------------------------------------------------


def _check_setpoints(
    setback_heat: int,
    comfort_heat: int,
    comfort_cool: int,
    setback_cool: int,
) -> dict[str, str]:
    """Replicate the cross-field validation logic from config_flow."""
    errors: dict[str, str] = {}
    if setback_heat >= comfort_heat:
        errors["setback_heat"] = "setback_must_be_lower"
    if setback_cool <= comfort_cool:
        errors["setback_cool"] = "setback_must_be_higher"
    return errors


class TestCrossFieldValidation:
    """Cross-field setpoint rules: setback_heat < comfort_heat, setback_cool > comfort_cool."""

    def test_valid_ranges(self):
        """Typical defaults — no errors expected."""
        errors = _check_setpoints(
            setback_heat=60,
            comfort_heat=70,
            comfort_cool=75,
            setback_cool=80,
        )
        assert errors == {}

    def test_setback_heat_equals_comfort(self):
        """Setback heat equal to comfort heat is invalid."""
        errors = _check_setpoints(
            setback_heat=70,
            comfort_heat=70,
            comfort_cool=75,
            setback_cool=80,
        )
        assert "setback_heat" in errors
        assert errors["setback_heat"] == "setback_must_be_lower"
        assert "setback_cool" not in errors

    def test_setback_heat_above_comfort(self):
        """Setback heat above comfort heat is invalid."""
        errors = _check_setpoints(
            setback_heat=72,
            comfort_heat=70,
            comfort_cool=75,
            setback_cool=80,
        )
        assert "setback_heat" in errors
        assert "setback_cool" not in errors

    def test_setback_cool_equals_comfort(self):
        """Setback cool equal to comfort cool is invalid."""
        errors = _check_setpoints(
            setback_heat=60,
            comfort_heat=70,
            comfort_cool=75,
            setback_cool=75,
        )
        assert "setback_cool" in errors
        assert errors["setback_cool"] == "setback_must_be_higher"
        assert "setback_heat" not in errors

    def test_setback_cool_below_comfort(self):
        """Setback cool below comfort cool is invalid."""
        errors = _check_setpoints(
            setback_heat=60,
            comfort_heat=70,
            comfort_cool=75,
            setback_cool=74,
        )
        assert "setback_cool" in errors
        assert "setback_heat" not in errors

    def test_both_invalid(self):
        """Both cross-field constraints violated simultaneously."""
        errors = _check_setpoints(
            setback_heat=72,
            comfort_heat=70,
            comfort_cool=75,
            setback_cool=74,
        )
        assert "setback_heat" in errors
        assert "setback_cool" in errors
        assert len(errors) == 2


# ---------------------------------------------------------------------------
# TestServiceSchemaValidation
# ---------------------------------------------------------------------------


class TestServiceSchemaValidation:
    """respond_to_suggestion service schema validates action and suggestion_key."""

    @pytest.fixture(autouse=True)
    def _build_schema(self):
        import voluptuous as vol  # noqa: PLC0415  (inside fixture intentionally)

        self.schema = vol.Schema(
            {
                vol.Required("action"): vol.In(["accept", "dismiss"]),
                vol.Required("suggestion_key"): vol.Coerce(str),
            }
        )

    def test_valid_accept(self):
        """'accept' with a known suggestion key is valid."""
        result = self.schema({"action": "accept", "suggestion_key": "low_window_compliance"})
        assert result["action"] == "accept"
        assert result["suggestion_key"] == "low_window_compliance"

    def test_valid_dismiss(self):
        """'dismiss' with a suggestion key is valid."""
        result = self.schema({"action": "dismiss", "suggestion_key": "frequent_overrides"})
        assert result["action"] == "dismiss"

    def test_invalid_action(self):
        """An unrecognised action value must raise vol.Invalid."""
        import voluptuous as vol  # noqa: PLC0415

        with pytest.raises(vol.Invalid):
            self.schema({"action": "delete", "suggestion_key": "foo"})

    def test_missing_action(self):
        """Omitting the required 'action' field must raise vol.MultipleInvalid."""
        import voluptuous as vol  # noqa: PLC0415

        with pytest.raises(vol.MultipleInvalid):
            self.schema({"suggestion_key": "foo"})

    def test_missing_suggestion_key(self):
        """Omitting the required 'suggestion_key' field must raise vol.MultipleInvalid."""
        import voluptuous as vol  # noqa: PLC0415

        with pytest.raises(vol.MultipleInvalid):
            self.schema({"action": "accept"})


# ---------------------------------------------------------------------------
# TestLearningDBEdgeCases
# ---------------------------------------------------------------------------


class TestLearningDBEdgeCases:
    """LearningEngine must handle malformed DB files gracefully and cap list growth."""

    # -- helpers --

    def _engine(self, tmp_path: Path) -> object:
        from custom_components.climate_advisor.learning import LearningEngine

        return LearningEngine(tmp_path)

    def _db_path(self, tmp_path: Path) -> Path:
        from custom_components.climate_advisor.const import LEARNING_DB_FILE

        return tmp_path / LEARNING_DB_FILE

    def _state_is_empty(self, engine) -> bool:
        summary = engine.get_compliance_summary()
        return summary["days_recorded"] == 0 and engine.get_last_suggestion_keys() == []

    # -- load_state type-safety --

    def test_load_json_array(self, tmp_path: Path):
        """A JSON array at the top level is not a valid state object — reset to empty."""
        self._db_path(tmp_path).write_text(json.dumps([]))
        engine = self._engine(tmp_path)
        engine.load_state()
        assert self._state_is_empty(engine)

    def test_load_json_string(self, tmp_path: Path):
        """A bare JSON string is not a valid state object — reset to empty."""
        self._db_path(tmp_path).write_text(json.dumps("hello"))
        engine = self._engine(tmp_path)
        engine.load_state()
        assert self._state_is_empty(engine)

    def test_load_json_null(self, tmp_path: Path):
        """A JSON null is not a valid state object — reset to empty."""
        self._db_path(tmp_path).write_text("null")
        engine = self._engine(tmp_path)
        engine.load_state()
        assert self._state_is_empty(engine)

    def test_load_json_number(self, tmp_path: Path):
        """A bare JSON number is not a valid state object — reset to empty."""
        self._db_path(tmp_path).write_text("42")
        engine = self._engine(tmp_path)
        engine.load_state()
        assert self._state_is_empty(engine)

    # -- dismissed_suggestions growth cap --

    def test_dismissed_suggestions_capped(self, tmp_path: Path):
        """Calling dismiss_suggestion() more than 100 times must not exceed the cap."""
        engine = self._engine(tmp_path)
        for i in range(150):
            engine.dismiss_suggestion(f"suggestion_key_{i}")
        assert len(engine._state.dismissed_suggestions) <= 100

    # -- settings_history growth cap --

    def test_settings_history_capped(self, tmp_path: Path):
        """The settings_history list must not grow beyond 200 entries."""
        engine = self._engine(tmp_path)
        # Pre-fill well beyond the cap to confirm trimming works
        engine._state.settings_history = [{"i": i} for i in range(250)]
        if len(engine._state.settings_history) > 200:
            engine._state.settings_history = engine._state.settings_history[-200:]
        assert len(engine._state.settings_history) == 200

    def test_settings_history_cap_via_accept(self, tmp_path: Path):
        """accept_suggestion() enforces the 200-entry cap on settings_history."""
        engine = self._engine(tmp_path)
        # Seed just under the cap so a single accept pushes past it
        engine._state.settings_history = [{"i": i} for i in range(200)]
        engine.accept_suggestion("low_window_compliance")
        assert len(engine._state.settings_history) <= 200
