"""Coverage of the dashboard REST API's remaining paths (quality scale Silver, test-coverage).

Every test drives the REAL ``HomeAssistantView`` subclass (``view.get(request)``/``view.post(request)``), as the
"Testing Sensor Attributes and API Views" rule in claude.md asks, and checks what the dashboard receives: the
status code, the body's fields, the coordinator call made (or not made).

Occupant view: these are the answers behind the dashboard's buttons and tabs. A wrong answer here is a button that
says "done" while nothing happened, a tab that shows another zone's data, or a crash instead of a clear message.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor import api
from custom_components.climate_advisor.const import TEMP_SOURCE_SENSOR
from tools.sim_harness.ha_stubs import set_loaded_zones


async def _run_executor(fn, *args):
    return fn(*args)


def _hass(coordinators: dict[str, Any] | None = None) -> MagicMock:
    hass = MagicMock()
    set_loaded_zones(hass, coordinators if coordinators is not None else {})
    hass.config.language = "en"
    hass.async_add_executor_job = _run_executor
    return hass


def _request(
    hass: MagicMock,
    *,
    method: str = "GET",
    query: dict[str, str] | None = None,
    body: Any = None,
    body_error: Exception | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    req = MagicMock()
    req.app = {"hass": hass}
    req.method = method
    req.path = "/api/climate_advisor/test"
    req.remote = "127.0.0.1"
    req.query = dict(query or {})
    req.rel_url.query = req.query
    req.headers = dict(headers or {})
    if body_error is not None:
        req.json = AsyncMock(side_effect=body_error)
    else:
        req.json = AsyncMock(return_value=body if body is not None else {})
    return req


def _coordinator(**attrs: Any) -> MagicMock:
    coord = MagicMock()
    coord.zone_label = "entry1"
    coord.config = {}
    for key, value in attrs.items():
        setattr(coord, key, value)
    return coord


def _call(view_cls, coordinator=None, **request_kwargs):
    """Run the view's handler for one loaded zone (``entry1``) and return ``(response, hass)``."""
    hass = _hass({"entry1": coordinator if coordinator is not None else _coordinator()})
    method = "post" if hasattr(view_cls, "post") else "get"
    request_kwargs.setdefault("method", method.upper())
    request = _request(hass, **request_kwargs)
    return asyncio.run(getattr(view_cls(), method)(request)), hass


def _views_with_method():
    out = []
    for view_cls in api.API_VIEWS:
        method = "post" if hasattr(view_cls, "post") else "get"
        out.append(pytest.param(view_cls, method, id=view_cls.__name__))
    return out


# ---------------------------------------------------------------------------
# The zone guard every view shares (Issues #796, #885, #1418)
# ---------------------------------------------------------------------------


class TestZoneGuardOnEveryView:
    """Every view refuses a request naming two zones and says "not loaded" while no zone is up."""

    @pytest.mark.parametrize(("view_cls", "method"), _views_with_method())
    def test_conflicting_zone_and_entry_id_is_refused(self, view_cls, method):
        """``?zone=a&entry_id=b``: answering for either zone could act on the wrong room, so 400 with the zone list."""
        coord_a, coord_b = _coordinator(zone_label="a"), _coordinator(zone_label="b")
        hass = _hass({"a": coord_a, "b": coord_b})
        request = _request(hass, method=method.upper(), query={"zone": "a", "entry_id": "b"})

        response = asyncio.run(getattr(view_cls(), method)(request))

        assert response.status == 400
        assert response.json_data["zone_selection_required"] is True
        assert response.json_data["zone_count"] == 2
        # Nothing was asked of either zone.
        assert coord_a.method_calls == [] and coord_b.method_calls == []

    @pytest.mark.parametrize(("view_cls", "method"), _views_with_method())
    def test_no_zone_loaded_answers_503_not_loaded(self, view_cls, method):
        """While Climate Advisor is still starting (no zone loaded) every view says so instead of failing."""
        hass = _hass({})
        request = _request(hass, method=method.upper())

        response = asyncio.run(getattr(view_cls(), method)(request))

        assert response.status == 503
        assert response.json_data["error_code"] == "not_loaded"

    def test_post_without_zone_on_two_zones_refuses_to_guess(self):
        """A control action with two zones and no zone named never picks one (Issue #885)."""
        coord_a, coord_b = _coordinator(zone_label="a"), _coordinator(zone_label="b")
        coord_a.automation_enabled = True
        coord_b.automation_enabled = True
        hass = _hass({"a": coord_a, "b": coord_b})

        response = asyncio.run(api.ClimateAdvisorToggleAutomationView().post(_request(hass, method="POST")))

        assert response.status == 400
        assert response.json_data["zone_selection_required"] is True
        coord_a.set_automation_enabled.assert_not_called()
        coord_b.set_automation_enabled.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_temp_source_msg_for_a_key_without_an_entity_setting(self):
        """A known source under a key that has no entity setting is just the source's label."""
        msg = api._temp_source_msg(MagicMock(), "some_other_source", TEMP_SOURCE_SENSOR, {})
        assert msg.key == f"settings.temp_source.{TEMP_SOURCE_SENSOR}"
        assert msg.params == {}

    def test_current_hold_payload_survives_a_failing_coordinator(self):
        """A failing hold summary never breaks the Status body: the field is just empty."""
        coord = MagicMock()
        coord.current_hold_payload.side_effect = RuntimeError("boom")
        assert api._current_hold_payload(coord) is None

    def test_current_hold_payload_ignores_a_non_dict(self):
        coord = MagicMock()
        coord.current_hold_payload.return_value = "not a dict"
        assert api._current_hold_payload(coord) is None
        coord.current_hold_payload.return_value = {"kind": "door"}
        assert api._current_hold_payload(coord) == {"kind": "door"}


# ---------------------------------------------------------------------------
# Read views
# ---------------------------------------------------------------------------


class TestBriefingView:
    def test_unknown_verbosity_is_400(self):
        response, _ = _call(api.ClimateAdvisorBriefingView, query={"verbosity": "shouty"})
        assert response.status == 400
        assert response.json_data["error_code"] == "invalid_verbosity"


class TestChartDataView:
    def test_bad_range_and_bad_before_ts_fall_back_to_defaults(self):
        """A bad range shows the last 24 h and a bad scroll position is ignored (never a 400)."""
        coord = _coordinator()
        coord.get_chart_data.return_value = {"points": [1, 2]}

        response, _ = _call(api.ClimateAdvisorChartDataView, coord, query={"range": "99y", "before_ts": "soon"})

        assert response.status == 200
        assert response.json_data == {"points": [1, 2]}
        coord.get_chart_data.assert_called_once_with(range_str="24h", before_ts=None)

    def test_before_ts_milliseconds_become_seconds(self):
        coord = _coordinator()
        coord.get_chart_data.return_value = {}

        _call(api.ClimateAdvisorChartDataView, coord, query={"range": "7d", "before_ts": "1700000000000"})

        coord.get_chart_data.assert_called_once_with(range_str="7d", before_ts=1_700_000_000.0)


class TestConfigView:
    def test_time_values_are_shown_as_text(self):
        """A bedtime stored as a time object reaches the Settings tab as ``22:30:00``, not an unserializable object."""
        coord = _coordinator(config={"sleep_time": _dt.time(22, 30)})

        response, _ = _call(api.ClimateAdvisorConfigView, coord)

        settings = {s["key"]: s for s in response.json_data["settings"]}
        assert settings["sleep_time"]["value"] == "22:30:00"


class TestAIStatusView:
    def test_client_status_never_carries_the_api_key_and_keeps_last_ten_requests(self):
        coord = _coordinator()
        coord.claude_client.get_status.return_value = {"status": "active", "api_key": "secret"}
        coord.claude_client.get_request_history.return_value = [{"n": i} for i in range(15)]

        response, _ = _call(api.ClimateAdvisorAIStatusView, coord)

        assert response.json_data["status"] == {"status": "active"}
        assert response.json_data["recent_requests"] == [{"n": i} for i in range(5, 15)]


class TestActivityRecordView:
    def _coord(self):
        coord = _coordinator()
        coord._event_log = []
        return coord

    def test_unreadable_hours_fall_back_to_12(self):
        response, _ = _call(api.ClimateAdvisorActivityRecordView, self._coord(), query={"hours": "lots"})
        assert response.json_data["hours"] == 12.0

    def test_footer_failure_still_returns_the_table(self, caplog):
        """The diagnostic footer is optional: if it fails the occupant still gets the activity table."""
        with (
            patch(
                "custom_components.climate_advisor.diagnostic_snapshot.build_diagnostic_snapshot",
                side_effect=RuntimeError("no snapshot"),
            ),
            caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.api"),
        ):
            response, _ = _call(api.ClimateAdvisorActivityRecordView, self._coord())

        assert response.status == 200
        assert isinstance(response.json_data["table"], str)
        assert "diagnostic footer failed" in caplog.text


class TestEventLogView:
    def test_unreadable_hours_and_failed_snapshot(self):
        """A bad ``hours`` shows 24 h and a failed snapshot leaves ``diagnostics`` empty, never a 500."""
        coord = _coordinator()
        coord._event_log = []
        with patch(
            "custom_components.climate_advisor.diagnostic_snapshot.async_build_diagnostic_snapshot",
            side_effect=RuntimeError("down"),
        ):
            response, _ = _call(api.ClimateAdvisorEventLogView, coord, query={"hours": "x"})

        assert response.status == 200
        assert response.json_data["hours"] == 24.0
        assert response.json_data["diagnostics"] is None
        assert response.json_data["events"] == []


class TestEnginesView:
    def test_learning_without_engine_status_is_503(self):
        coord = _coordinator(learning=object())
        response, _ = _call(api.ClimateAdvisorEnginesView, coord)
        assert response.status == 503
        assert response.json_data["error_code"] == "engine_status_unavailable"

    def test_coordinator_without_v4_or_decision_summaries(self):
        """An older coordinator without the v4 / decision summaries still answers, without those sections."""
        coord = _coordinator(config={"temp_unit": "celsius"})
        coord.learning.get_engine_status.return_value = {"engines": []}
        del coord.v4_model_summary
        del coord.decision_rates_summary

        response, _ = _call(api.ClimateAdvisorEnginesView, coord)

        assert response.json_data == {"engines": [], "unit": "celsius"}


class TestPredictionAccuracyView:
    def test_naive_before_gets_the_local_timezone(self):
        coord = _coordinator(config={})
        coord.v4_accuracy_inputs.return_value = {"config_dir": "/config", "zone": "entry1"}
        seen: dict[str, Any] = {}

        def _build(**kwargs):
            seen.update(kwargs)
            return {"ok": True}

        with (
            patch.object(api.prediction_accuracy, "build_prediction_accuracy", side_effect=_build),
            patch.object(api.prediction_accuracy, "load_backtest", return_value=None),
        ):
            response, _ = _call(
                api.ClimateAdvisorPredictionAccuracyView, coord, query={"before": "2026-07-01T12:00:00"}
            )

        assert response.json_data == {"ok": True}
        assert seen["before"].tzinfo is not None
        assert seen["before"].replace(tzinfo=None) == _dt.datetime(2026, 7, 1, 12, 0)


# ---------------------------------------------------------------------------
# Action views
# ---------------------------------------------------------------------------


class TestRespondSuggestionView:
    def test_unreadable_body_is_400(self):
        coord = _coordinator()
        response, _ = _call(api.ClimateAdvisorRespondSuggestionView, coord, body_error=ValueError("bad json"))
        assert response.status == 400
        assert response.json_data["error_code"] == "invalid_json_body"
        coord.learning.dismiss_suggestion.assert_not_called()


class TestCancelOverrideView:
    def test_nothing_to_cancel_says_so_and_schedules_nothing(self):
        coord = _coordinator()
        coord.automation_engine.cancel_override.return_value = False
        coord._unsub_listeners = []

        with patch.object(api, "async_call_later") as call_later:
            response, _ = _call(api.ClimateAdvisorCancelOverrideView, coord)

        assert response.json_data["message_msg"]["key"] == "errors.cancel_nothing"
        call_later.assert_not_called()
        assert coord._unsub_listeners == []

    def test_follow_up_reapplies_todays_plan(self):
        """10 s after a cancel, the day's plan is applied again (with the planned window close carried, #1231)."""
        coord = _coordinator()
        ae = coord.automation_engine
        ae.cancel_override.return_value = True
        ae._occupant_override_active.return_value = True
        ae.apply_classification = AsyncMock()
        ae._nat_vent_cutoff = "cutoff"
        ae._nat_vent_cutoff_already_reached = True
        coord._current_classification = "today's plan"
        coord._unsub_listeners = []
        captured: dict[str, Any] = {}

        def _call_later(hass, delay, fn):
            captured["delay"], captured["fn"] = delay, fn
            return "unsub-handle"

        with patch.object(api, "async_call_later", side_effect=_call_later):
            response, hass = _call(api.ClimateAdvisorCancelOverrideView, coord)

        assert response.json_data["message_msg"]["params"]["what"] == "override"
        assert captured["delay"] == 10
        assert coord._unsub_listeners == ["unsub-handle"]

        tasks: list[Any] = []
        hass.async_create_task = MagicMock(side_effect=tasks.append)
        captured["fn"](None)
        assert len(tasks) == 1
        asyncio.run(tasks[0])
        ae.apply_classification.assert_awaited_once_with(
            "today's plan", nat_vent_cutoff="cutoff", nat_vent_cutoff_already_reached=True
        )

    def test_follow_up_without_a_plan_does_nothing(self):
        coord = _coordinator()
        coord.automation_engine.cancel_override.return_value = True
        coord._current_classification = None
        coord._unsub_listeners = []
        captured: dict[str, Any] = {}

        with patch.object(api, "async_call_later", side_effect=lambda h, d, fn: captured.setdefault("fn", fn)):
            _, hass = _call(api.ClimateAdvisorCancelOverrideView, coord)

        hass.async_create_task = MagicMock()
        captured["fn"](None)
        hass.async_create_task.assert_not_called()


class TestResumeFromPauseView:
    def _coord(self, *, paused=True, dry_run=False, pending=False):
        coord = _coordinator()
        ae = coord.automation_engine
        ae.is_paused_by_door = paused
        ae.dry_run = dry_run
        ae.resume_restore_pending = pending
        ae.resume_from_pause = AsyncMock(return_value="heat")
        return coord

    def test_not_paused_changes_nothing(self):
        coord = self._coord(paused=False)
        response, _ = _call(api.ClimateAdvisorResumeFromPauseView, coord)
        assert response.json_data["message_msg"]["key"] == "errors.resume_not_paused"
        coord.automation_engine.resume_from_pause.assert_not_awaited()

    def test_observe_only_resume_sends_no_restore(self):
        coord = self._coord(dry_run=True)
        response, _ = _call(api.ClimateAdvisorResumeFromPauseView, coord)
        assert response.json_data["message_msg"]["key"] == "errors.resume_observe_only"
        assert response.json_data["restored_mode"] is None

    def test_restore_not_landed_yet_is_reported_pending(self):
        coord = self._coord(pending=True)
        response, _ = _call(api.ClimateAdvisorResumeFromPauseView, coord)
        assert response.json_data["message_msg"]["key"] == "errors.resume_pending"
        assert response.json_data["restored_mode"] == "heat"
        assert response.json_data["restore_pending"] is True

    def test_resume_done(self):
        coord = self._coord()
        response, _ = _call(api.ClimateAdvisorResumeFromPauseView, coord)
        assert response.json_data["message_msg"] == {"key": "errors.resume_done", "params": {"mode": "heat"}}
        assert response.json_data["restored_mode"] == "heat"
        assert "restore_pending" not in response.json_data


class TestDeleteReportView:
    def test_unreadable_body(self):
        response, _ = _call(api.ClimateAdvisorDeleteReportView, body_error=ValueError("x"))
        assert response.status == 400
        assert response.json_data["error"] == "invalid_json"

    def test_missing_timestamp(self):
        response, _ = _call(api.ClimateAdvisorDeleteReportView, body={"report_type": "investigation"})
        assert response.status == 400
        assert response.json_data["error"] == "missing_timestamp"

    def test_unknown_type(self):
        coord = _coordinator()
        response, _ = _call(api.ClimateAdvisorDeleteReportView, coord, body={"report_type": "other", "timestamp": "t"})
        assert response.status == 400
        assert response.json_data["error"] == "invalid_type"
        coord.delete_investigation_report.assert_not_called()

    @pytest.mark.parametrize("found", [True, False])
    def test_investigation_deleted_and_saved_only_when_found(self, found):
        coord = _coordinator()
        coord.delete_investigation_report.return_value = found
        response, _ = _call(
            api.ClimateAdvisorDeleteReportView, coord, body={"report_type": "investigation", "timestamp": "t1"}
        )
        assert response.json_data == {"success": True, "found": found}
        coord.delete_investigation_report.assert_called_once_with("t1")
        assert coord._save_investigation_reports.call_count == (1 if found else 0)


class TestSubmitGithubIssueView:
    _CONFIG = {"github_token": "tok", "github_repo": "owner/repo"}

    def _coord(self, config=None):
        return _coordinator(config=dict(self._CONFIG if config is None else config), _entry_id="entry1")

    @pytest.fixture(autouse=True)
    def _aiohttp_client_module(self, monkeypatch):
        """Home Assistant's real accessor module (absent from the stub), as test_ai_investigator.py provides it."""
        module = types.ModuleType("homeassistant.helpers.aiohttp_client")
        module.async_get_clientsession = lambda hass: self._session
        monkeypatch.setitem(sys.modules, "homeassistant.helpers.aiohttp_client", module)

    def _post(self, coord, session=None, **kwargs):
        self._session = session
        return _call(api.ClimateAdvisorSubmitGithubIssueView, coord, **kwargs)[0]

    def test_not_configured(self):
        response = self._post(self._coord(config={}), body={"title": "t"})
        assert response.json_data["success"] is False
        assert response.json_data["error"] == "not_configured"

    def test_unreadable_body(self):
        response = self._post(self._coord(), body_error=ValueError("x"))
        assert response.status == 400
        assert response.json_data["error"] == "invalid_json"

    def test_missing_title(self):
        response = self._post(self._coord(), body={"title": "   "})
        assert response.status == 400
        assert response.json_data["error"] == "missing_title"

    def test_created_issue_and_bad_labels_become_bug(self):
        session = MagicMock()
        resp = MagicMock(status=201)
        resp.json = AsyncMock(return_value={"html_url": "https://github.com/o/r/issues/7", "number": 7})
        session.post = AsyncMock(return_value=resp)

        response = self._post(self._coord(), session, body={"title": " T ", "body": "B", "labels": "bug,ui"})

        assert response.json_data == {
            "success": True,
            "issue_url": "https://github.com/o/r/issues/7",
            "issue_number": 7,
        }
        args, kwargs = session.post.call_args
        assert args == ("https://api.github.com/repos/owner/repo/issues",)
        assert kwargs["json"] == {"title": "T", "body": "B", "labels": ["bug"]}

    def test_network_error(self, caplog):
        session = MagicMock()
        session.post = AsyncMock(side_effect=OSError("offline"))
        with caplog.at_level(logging.ERROR, logger="custom_components.climate_advisor.api"):
            response = self._post(self._coord(), session, body={"title": "T"})
        assert response.json_data["success"] is False
        assert response.json_data["error"] == "network_error"
        assert "tok" not in caplog.text

    def test_api_error_status(self):
        session = MagicMock()
        session.post = AsyncMock(return_value=MagicMock(status=422))
        response = self._post(self._coord(), session, body={"title": "T"})
        assert response.json_data["error"] == "github_api_422"
        assert response.json_data["error_msg"]["params"] == {"status": 422}

    def test_rejected_token_asks_the_owner_for_a_new_one(self):
        """GitHub 401: the token was revoked, so a reauth is raised for this zone's entry (Issue #1643)."""
        session = MagicMock()
        session.post = AsyncMock(return_value=MagicMock(status=401))
        coord = self._coord()
        with patch("custom_components.climate_advisor.reauth.async_request_reauth") as reauth:
            response = self._post(coord, session, body={"title": "T"})
        assert response.json_data["error"] == "github_api_401"
        reauth.assert_called_once()
        assert reauth.call_args.args[2] == "github_token"


# ---------------------------------------------------------------------------
# AI investigation
# ---------------------------------------------------------------------------


def _investigate_coord(result: dict | None = None) -> MagicMock:
    coord = _coordinator(config={"ai_enabled": True, "ai_investigator_enabled": True})
    coord.claude_client.check_investigator_rate_limit.return_value = (True, "")
    coord.ai_skills.async_execute = AsyncMock(return_value=result if result is not None else {"success": True})
    coord.async_store_investigation_report = AsyncMock()
    return coord


class TestInvestigateView:
    def test_unreadable_body_and_bad_hours_use_defaults(self):
        coord = _investigate_coord()
        _call(api.ClimateAdvisorInvestigateView, coord, body_error=ValueError("no body"))
        kwargs = coord.ai_skills.async_execute.await_args.kwargs
        assert (kwargs["focus"], kwargs["hours"], kwargs["deep"]) == ("", 24, False)

    def test_non_numeric_hours_use_24(self):
        coord = _investigate_coord()
        _call(api.ClimateAdvisorInvestigateView, coord, body={"hours": "a day"})
        assert coord.ai_skills.async_execute.await_args.kwargs["hours"] == 24

    def test_empty_visible_output_is_stored_with_a_warning(self, caplog):
        coord = _investigate_coord({"success": True, "truncated_empty": True, "data": {}})
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.api"):
            response, _ = _call(api.ClimateAdvisorInvestigateView, coord, body={})
        assert response.status == 200
        assert "zero visible output" in caplog.text
        coord.async_store_investigation_report.assert_awaited_once()

    def test_failure_with_the_investigators_text_is_500(self):
        coord = _investigate_coord({"success": False, "error": "Provider said no"})
        response, _ = _call(api.ClimateAdvisorInvestigateView, coord, body={})
        assert response.status == 500
        assert response.json_data == {"message": "Provider said no"}
        coord.async_store_investigation_report.assert_not_awaited()

    def test_failure_without_text_is_a_translated_500(self):
        coord = _investigate_coord({"success": False})
        response, _ = _call(api.ClimateAdvisorInvestigateView, coord, body={})
        assert response.status == 500
        assert response.json_data["message_msg"]["key"] == "errors.investigation_failed"
        coord.claude_client.increment_investigator_counter.assert_not_called()


class _FakeStream:
    """Stand-in for ``aiohttp.web.StreamResponse``: records what the dashboard's event stream receives."""

    instances: list[_FakeStream] = []

    def __init__(self, headers=None):
        self.headers = headers
        self.content_type = None
        self.chunks: list[bytes] = []
        self.prepared = False
        self.eof = False
        _FakeStream.instances.append(self)

    async def prepare(self, request):
        self.prepared = True

    async def write(self, data: bytes):
        self.chunks.append(data)

    async def drain(self):
        return None

    async def write_eof(self):
        self.eof = True


class TestInvestigateStreaming:
    def _stream(self, events, caplog=None):
        coord = _investigate_coord()

        async def _gen(*args, **kwargs):
            for event in events:
                yield dict(event)

        coord.ai_skills.async_execute_streaming = _gen
        _FakeStream.instances.clear()
        with patch.object(api.web, "StreamResponse", _FakeStream):
            response, _ = _call(
                api.ClimateAdvisorInvestigateView, coord, body={}, headers={"Accept": "text/event-stream"}
            )
        return coord, response

    def test_chunks_then_done_are_streamed_and_the_report_stored(self):
        import json as _json

        coord, response = self._stream(
            [
                {"type": "chunk", "text": "a"},
                {"type": "chunk", "text": "b"},
                {"type": "done", "success": True, "data": {"summary": "s"}},
            ]
        )

        assert response is _FakeStream.instances[0]
        assert response.prepared and response.eof
        assert response.content_type == "text/event-stream"
        events = [_json.loads(c.decode()[len("data: ") :]) for c in response.chunks]
        assert [e["type"] for e in events] == ["chunk", "chunk", "done"]
        assert events[-1]["cut_short"] is False
        stored = coord.async_store_investigation_report.await_args.args[0]
        assert stored["success"] is True and stored["data"] == {"summary": "s"}
        coord.claude_client.increment_investigator_counter.assert_called_once()

    @pytest.mark.parametrize(
        ("done", "expected"),
        [
            ({"truncated_empty": True}, "zero visible output"),
            ({"truncated": True}, "hit max_tokens limit"),
            ({"data": {"full_text": "partial"}}, "required sections missing"),
        ],
    )
    def test_incomplete_reports_are_flagged(self, done, expected, caplog):
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.api"):
            coord, _ = self._stream([{"type": "done", "success": True, **done}])
        assert expected in caplog.text
        coord.async_store_investigation_report.assert_awaited_once()

    def test_failed_stream_is_not_stored(self):
        coord, response = self._stream([{"type": "done", "success": False}])
        assert response.eof
        coord.async_store_investigation_report.assert_not_awaited()
        coord.claude_client.increment_investigator_counter.assert_not_called()

    def test_stream_with_no_done_event_is_not_stored(self):
        coord, response = self._stream([{"type": "chunk", "text": "a"}, {"type": "status", "text": "thinking"}])
        assert len(response.chunks) == 2
        coord.async_store_investigation_report.assert_not_awaited()
