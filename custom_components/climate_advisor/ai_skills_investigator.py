"""Investigative Agent AI skill for Climate Advisor (Issue #82)."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .ai_skills import AISkillDefinition, AISkillRegistry
from .const import (
    ATTR_AUTOMATION_STATUS,
    ATTR_CONTACT_STATUS,
    ATTR_DAY_TYPE,
    ATTR_FAN_STATUS,
    ATTR_HVAC_ACTION,
    ATTR_LAST_ACTION_REASON,
    ATTR_LAST_ACTION_TIME,
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
    ATTR_OCCUPANCY_MODE,
    ATTR_TREND,
    CONF_AI_INVESTIGATOR_MAX_TOKENS,
    CONF_AI_INVESTIGATOR_MODEL,
    CONF_AI_INVESTIGATOR_REASONING,
)

_LOGGER = logging.getLogger(__name__)

_SKILL_NAME = "investigator"

_SYSTEM_PROMPT = """\
You are a scientific investigator for Climate Advisor, a Home Assistant HVAC automation integration.\
 Your job is to find incongruities, data quality problems, and system errors by cross-referencing\
 all available data sources.

EPISTEMOLOGICAL DISCIPLINE
NUMERIC VERIFICATION RULE: Before stating that any temperature, percentage, or count
value is "within," "inside," "in range," or similar, verify the arithmetic explicitly.
A temperature T is within comfort band [L, H] only if L <= T <= H. Never infer
"within range" from proximity or narrative context — check the inequality directly
against the supplied numeric values. If you cannot verify the claim with the supplied
data, say "cannot verify" rather than guessing.

Always be explicit about the category of every claim you make:
- CONFIRMED FACT: the value is directly present in the supplied data
- INFERENCE: a conclusion deduced from a pattern across multiple data points
- ASSUMPTION: a guess made in the absence of direct evidence — always label these

INVESTIGATION PROCEDURE
1. Read all supplied data sections before drawing any conclusion.
2. Cross-check AI summary text against the raw numeric data. Where they disagree, flag the\
 discrepancy with exact values from both sources.
3. Scan for suspicious or impossible values, including but not limited to:
   - Window compliance reported as 0% on days when windows were physically opened
   - Override counts that are implausibly high (>50 in a short window)
   - Timestamps that are in the future, in the wrong timezone, or precede the system installation
   - Zeroed counters that should accumulate over time (runtime, observation counts)
   - Thermal rates (heating/cooling °F per hour) outside physically plausible bounds
   - Weather bias corrections that exceed the configured cap
4. Check the event log for any entries whose type contains "error" or "warning". Quote the\
 relevant event fields verbatim.
5. Generate 2–5 ranked hypotheses about what may be wrong or inconsistent. Rank by confidence\
 (highest first). Each hypothesis must cite at least one evidence item.
6. For every cited data value use the format: [source: <data_key>, value: <X>]
7. Where data is missing or unavailable, state explicitly: "Could not verify <X> — data not\
 present."

OUTPUT FORMAT
Return your investigation using these exact section headers (## prefix, exact capitalisation):

## INVESTIGATION SUMMARY
2–4 sentence overview of the most important finding. If nothing is wrong, say so plainly\
 — do not fabricate issues.

## INCONGRUITIES FOUND
List every place where two data sources contradict each other. Use bullet points. If none,\
 write "None detected."

## DATA QUALITY ISSUES
List missing fields, implausible values, zeroed counters, timestamp anomalies, etc. Use\
 bullet points. If none, write "None detected."

## SYSTEM ERRORS / WARNINGS
Quote or paraphrase every event log entry with type containing "error" or "warning". Include\
 the timestamp and event type. If none, write "No errors or warnings in the supplied window."

## HYPOTHESES
Numbered list, ranked highest-confidence first. Each entry: hypothesis text, confidence\
 (High / Medium / Low), and supporting evidence citations [source: ..., value: ...].

## RECOMMENDED ACTIONS
Concrete steps to resolve each identified issue. Map each action to the relevant hypothesis\
 or finding number.

## ASSUMPTIONS & CONFIDENCE
List every assumption made during this investigation and your overall confidence that the\
 analysis is complete given the available data.

TONE
Scientific, evidence-based, methodical. Prefer "no evidence of X" over "X is fine". Never\
 fabricate data or invent explanations — if the data does not support a conclusion, say so.\
"""


def _build_version_context(coordinator) -> str:
    """Build version/release notes section for investigator context."""
    from .const import RELEASE_NOTES, VERSION  # noqa: PLC0415

    lines = [f"## RUNNING VERSION\n{VERSION}\n"]
    lines.append("## RECENT RELEASE NOTES")
    for ver, notes in list(RELEASE_NOTES.items())[:5]:
        lines.append(f"\n### v{ver}")
        for note in notes:
            lines.append(f"- {note}")
    return "\n".join(lines)


async def async_build_github_context(hass) -> str:
    """Fetch recent GitHub issues for investigator context. Returns '' on any error."""
    import aiohttp  # noqa: PLC0415

    from .const import (  # noqa: PLC0415
        GITHUB_API_BASE,
        GITHUB_CONTEXT_TIMEOUT,
        GITHUB_ISSUES_LIMIT,
        GITHUB_REPO,
        GITHUB_REPO_URL,
    )

    try:
        session = hass.helpers.aiohttp_client.async_get_clientsession()
        url = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/issues?state=all&per_page={GITHUB_ISSUES_LIMIT}&sort=updated"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=GITHUB_CONTEXT_TIMEOUT)) as resp:
            if resp.status != 200:
                return ""
            issues = await resp.json()
        lines = [f"## GITHUB REPOSITORY\n{GITHUB_REPO_URL}\n", "## RECENT GITHUB ISSUES"]
        for issue in issues:
            state = issue.get("state", "?")
            number = issue.get("number", "?")
            title = issue.get("title", "")[:100]
            labels = ", ".join(lbl["name"] for lbl in issue.get("labels", []))
            label_str = f" [{labels}]" if labels else ""
            lines.append(f"- #{number} ({state}){label_str}: {title}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


async def async_build_investigator_context(
    hass: HomeAssistant,
    coordinator: Any,
    **kwargs: Any,
) -> str:
    """Build context string for the investigator skill.

    Gathers current state, learning data, event log, AI report history, and config
    from the coordinator and HA, then formats them as a structured multi-section text
    block suitable for Claude cross-source analysis.

    Each data source is fetched inside its own try/except so a failure in one section
    never prevents the others from being included.
    """
    lines: list[str] = ["=== Climate Advisor Investigator Context ===", ""]

    # Focus question (optional caller override)
    focus: str = kwargs.get("focus", "")
    if focus:
        lines += [
            "=== INVESTIGATION FOCUS (USER-DIRECTED) ===",
            f"  {focus}",
            "",
        ]

    # ------------------------------------------------------------------
    # 1. Current state from coordinator.data
    # ------------------------------------------------------------------
    try:
        data: dict[str, Any] = coordinator.data or {}
        day_type = data.get(ATTR_DAY_TYPE, "unknown")
        trend = data.get(ATTR_TREND, "unknown")
        hvac_action = data.get(ATTR_HVAC_ACTION, "unknown")
        # Compute fresh runtime — coordinator.data may be up to 30 min stale
        _base_runtime = coordinator._today_record.hvac_runtime_minutes if coordinator._today_record is not None else 0.0
        _session_elapsed = (
            (dt_util.now() - coordinator._hvac_on_since).total_seconds() / 60.0
            if coordinator._hvac_on_since is not None
            else 0.0
        )
        hvac_runtime_today = round(_base_runtime + _session_elapsed, 1)
        automation_status = data.get(ATTR_AUTOMATION_STATUS, "unknown")
        last_action_time = data.get(ATTR_LAST_ACTION_TIME, "unknown")
        last_action_reason = data.get(ATTR_LAST_ACTION_REASON, "unknown")
        next_action = data.get(ATTR_NEXT_AUTOMATION_ACTION, "unknown")
        next_action_time = data.get(ATTR_NEXT_AUTOMATION_TIME, "unknown")
        occupancy_mode = data.get(ATTR_OCCUPANCY_MODE, "unknown")
        fan_status = data.get(ATTR_FAN_STATUS, "unknown")
        contact_status = data.get(ATTR_CONTACT_STATUS, "unknown")

        lines += [
            "=== CURRENT STATE ===",
            f"  day_type:            {day_type}",
            f"  trend:               {trend}",
            f"  hvac_action:         {hvac_action}",
            f"  hvac_runtime_today:  {hvac_runtime_today} min",
            f"  automation_status:   {automation_status}",
            f"  last_action_time:    {last_action_time}",
            f"  last_action_reason:  {last_action_reason}",
            f"  next_action:         {next_action}",
            f"  next_action_time:    {next_action_time}",
            f"  occupancy_mode:      {occupancy_mode}",
            f"  fan_status:          {fan_status}",
            f"  contact_status:      {contact_status}",
            "",
        ]
    except Exception:
        _LOGGER.warning("investigator: failed to read coordinator.data — skipping current state")
        lines += ["=== CURRENT STATE ===", "  unavailable", ""]

    # ------------------------------------------------------------------
    # 2. HVAC entity state from HA
    # ------------------------------------------------------------------
    try:
        climate_entity_id: str = (coordinator.config or {}).get("climate_entity", "")
        hvac_mode = "unknown"
        current_temp = "unknown"
        if climate_entity_id:
            climate_state = hass.states.get(climate_entity_id)
            if climate_state is not None:
                hvac_mode = climate_state.state
                current_temp = climate_state.attributes.get("current_temperature", "unknown")

        lines += [
            "=== HVAC ENTITY ===",
            f"  entity_id:     {climate_entity_id or 'not configured'}",
            f"  hvac_mode:     {hvac_mode}",
            f"  current_temp:  {current_temp}",
            "",
        ]
    except Exception:
        _LOGGER.warning("investigator: failed to read HVAC entity state — skipping")
        lines += ["=== HVAC ENTITY ===", "  unavailable", ""]

    # ------------------------------------------------------------------
    # 3. Learning engine data
    # ------------------------------------------------------------------
    try:
        learning = coordinator.learning if hasattr(coordinator, "learning") else None
        if learning is not None:
            # Compliance summary
            try:
                compliance: dict[str, Any] = learning.get_compliance_summary() or {}
                lines += [
                    "=== LEARNING — COMPLIANCE SUMMARY ===",
                    f"  window_compliance:              {compliance.get('window_compliance', 'unknown')}",
                    f"  avg_daily_hvac_runtime_minutes: {compliance.get('avg_daily_hvac_runtime_minutes', 'unknown')}",
                    f"  comfort_score:                  {compliance.get('comfort_score', 'unknown')}",
                    f"  total_manual_overrides:         {compliance.get('total_manual_overrides', 'unknown')}",
                    f"  pending_suggestions:            {compliance.get('pending_suggestions', 'unknown')}",
                    "",
                ]
            except Exception:
                _LOGGER.warning("investigator: get_compliance_summary() failed")
                lines += ["=== LEARNING — COMPLIANCE SUMMARY ===", "  unavailable", ""]

            # Thermal model
            try:
                thermal: dict[str, Any] = learning.get_thermal_model() or {}
                lines += [
                    "=== LEARNING — THERMAL MODEL ===",
                    f"  heating_rate_f_per_hour:   {thermal.get('heating_rate_f_per_hour', 'unknown')}",
                    f"  cooling_rate_f_per_hour:   {thermal.get('cooling_rate_f_per_hour', 'unknown')}",
                    f"  confidence:                {thermal.get('confidence', 'unknown')}",
                    f"  observation_count_heat:    {thermal.get('observation_count_heat', 'unknown')}",
                    f"  observation_count_cool:    {thermal.get('observation_count_cool', 'unknown')}",
                    "",
                ]
            except Exception:
                _LOGGER.warning("investigator: get_thermal_model() failed")
                lines += ["=== LEARNING — THERMAL MODEL ===", "  unavailable", ""]

            # Weather bias
            try:
                bias: dict[str, Any] = learning.get_weather_bias() or {}
                lines += [
                    "=== LEARNING — WEATHER BIAS ===",
                    f"  high_bias:          {bias.get('high_bias', 'unknown')}",
                    f"  low_bias:           {bias.get('low_bias', 'unknown')}",
                    f"  confidence:         {bias.get('confidence', 'unknown')}",
                    f"  observation_count:  {bias.get('observation_count', 'unknown')}",
                    "",
                ]
            except Exception:
                _LOGGER.warning("investigator: get_weather_bias() failed")
                lines += ["=== LEARNING — WEATHER BIAS ===", "  unavailable", ""]

            # Active suggestions
            try:
                suggestions: list[Any] = learning.generate_suggestions() or []
                lines.append("=== LEARNING — ACTIVE SUGGESTIONS ===")
                if suggestions:
                    for idx, sug in enumerate(suggestions, start=1):
                        if isinstance(sug, dict):
                            stype = sug.get("suggestion_type", "unknown")
                            text = sug.get("text", "")
                            evidence = sug.get("evidence", {})
                            lines.append(f"  [{idx}] type={stype}")
                            if text:
                                lines.append(f"      text: {text}")
                            if evidence:
                                lines.append(f"      evidence: {evidence}")
                else:
                    lines.append("  (none)")
                lines.append("")
            except Exception:
                _LOGGER.warning("investigator: generate_suggestions() failed")
                lines += ["=== LEARNING — ACTIVE SUGGESTIONS ===", "  unavailable", ""]

            # Last 14 daily records
            try:
                state_obj = getattr(learning, "_state", None)
                records: list[Any] = []
                if state_obj is not None:
                    raw_records = getattr(state_obj, "records", None)
                    if isinstance(raw_records, list):
                        records = raw_records[-14:]

                lines.append("=== LEARNING — LAST 14 DAILY RECORDS ===")
                if records:
                    for rec in records:
                        if isinstance(rec, dict):
                            date_val = rec.get("date", "?")
                            recommended = rec.get("windows_recommended", False)
                            opened = rec.get("windows_physically_opened", rec.get("windows_opened", False))
                            compliance_val = ("opened" if opened else "not-opened") if recommended else "n/a"
                            runtime = rec.get("hvac_runtime_minutes", "?")
                            overrides = rec.get("manual_overrides", "?")
                            lines.append(
                                f"  {date_val}: opened={opened} window_rec={compliance_val}"
                                f" runtime={runtime}min overrides={overrides}"
                            )
                else:
                    lines.append("  (no records)")
                lines.append("")
            except Exception:
                _LOGGER.warning("investigator: failed to read daily records")
                lines += ["=== LEARNING — LAST 14 DAILY RECORDS ===", "  unavailable", ""]
        else:
            lines += ["=== LEARNING ===", "  learning engine not available", ""]
    except Exception:
        _LOGGER.warning("investigator: failed to access learning engine — skipping")
        lines += ["=== LEARNING ===", "  unavailable", ""]

    # ------------------------------------------------------------------
    # 4. Event log
    # ------------------------------------------------------------------
    try:
        hours: int = int(kwargs.get("hours", 48))
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)
        event_log: list[Any] = getattr(coordinator, "_event_log", []) or []
        recent_events: list[Any] = []

        for entry in event_log[-200:]:
            if not isinstance(entry, dict):
                continue
            raw_time = entry.get("time")
            if raw_time is None:
                recent_events.append(entry)
                continue
            # Accept datetime objects or ISO strings
            if isinstance(raw_time, datetime.datetime):
                event_dt = raw_time
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=datetime.UTC)
            else:
                try:
                    event_dt = datetime.datetime.fromisoformat(str(raw_time))
                    if event_dt.tzinfo is None:
                        event_dt = event_dt.replace(tzinfo=datetime.UTC)
                except ValueError:
                    recent_events.append(entry)
                    continue
            if event_dt >= cutoff:
                recent_events.append(entry)

        # Count by type
        type_counts: dict[str, int] = {}
        errors_and_warnings: list[dict[str, Any]] = []
        for entry in recent_events:
            etype = str(entry.get("type", "unknown"))
            type_counts[etype] = type_counts.get(etype, 0) + 1
            if "error" in etype.lower() or "warning" in etype.lower():
                errors_and_warnings.append(entry)

        lines += [
            f"=== EVENT LOG (last {hours}h, {len(recent_events)} events) ===",
            f"  event_type_counts: {type_counts}",
            f"  errors_and_warnings_count: {len(errors_and_warnings)}",
        ]
        if errors_and_warnings:
            lines.append("  ERROR/WARNING ENTRIES:")
            for entry in errors_and_warnings:
                lines.append(f"    {entry}")
        lines.append("")
    except Exception:
        _LOGGER.warning("investigator: failed to read event log — skipping")
        lines += ["=== EVENT LOG ===", "  unavailable", ""]

    # ------------------------------------------------------------------
    # 5. Recent AI report history
    # ------------------------------------------------------------------
    try:
        report_history_fn = getattr(coordinator, "get_ai_report_history", None)
        if callable(report_history_fn):
            report_history: list[Any] = report_history_fn() or []
            last_reports = report_history[-3:]
            lines.append("=== RECENT AI ACTIVITY REPORTS (last 3) ===")
            if last_reports:
                for rpt in last_reports:
                    if isinstance(rpt, dict):
                        ts = rpt.get("timestamp", "unknown")
                        result = rpt.get("result", {})
                        summary_text = result.get("data", {}).get("summary", "") if isinstance(result, dict) else ""
                        lines.append(f"  [{ts}] summary: {summary_text or '(no summary)'}")
            else:
                lines.append("  (no prior reports)")
            lines.append("")
        else:
            lines += ["=== RECENT AI ACTIVITY REPORTS ===", "  get_ai_report_history not available", ""]
    except Exception:
        _LOGGER.warning("investigator: failed to read AI report history — skipping")
        lines += ["=== RECENT AI ACTIVITY REPORTS ===", "  unavailable", ""]

    # ------------------------------------------------------------------
    # 6. Config (sensitive keys stripped)
    # ------------------------------------------------------------------
    try:
        cfg: dict[str, Any] = dict(coordinator.config or {})
        cfg.pop("ai_api_key", None)

        _comfort_heat = cfg.get("comfort_heat", "unknown")
        _comfort_cool = cfg.get("comfort_cool", "unknown")
        lines += [
            "=== CONFIGURATION ===",
            f"  comfort_heat (lower bound): {_comfort_heat} — indoor must be >= this to be in comfort band",
            f"  comfort_cool (upper bound): {_comfort_cool} — indoor must be <= this to be in comfort band",
            f"  comfort_band: [{_comfort_heat}, {_comfort_cool}]°F"
            " — temperature T is in-band only if comfort_heat <= T <= comfort_cool",
            f"  setback_heat:    {cfg.get('setback_heat', 'unknown')}",
            f"  setback_cool:    {cfg.get('setback_cool', 'unknown')}",
            f"  wake_time:       {cfg.get('wake_time', 'unknown')}",
            f"  sleep_time:      {cfg.get('sleep_time', 'unknown')}",
            f"  briefing_time:   {cfg.get('briefing_time', 'unknown')}",
            f"  ai_enabled:      {cfg.get('ai_enabled', 'unknown')}",
            f"  ai_model:        {cfg.get('ai_model', 'unknown')}",
            f"  learning_enabled:{cfg.get('learning_enabled', 'unknown')}",
            "",
        ]
    except Exception:
        _LOGGER.warning("investigator: failed to read config — skipping")
        lines += ["=== CONFIGURATION ===", "  unavailable", ""]

    # ------------------------------------------------------------------
    # 7. CA operational design — prevents the AI from hallucinating
    #    explanations for states that CA itself controls (#113)
    # ------------------------------------------------------------------
    lines += [
        "=== CA OPERATIONAL DESIGN ===",
        "CA has 100% programmatic control of the HVAC via HA service calls.",
        "There is NO physical switch that can activate the fan independently.",
        "If the fan is running, one of the following is true:",
        "  - CA activated it (fan_status=active, natural vent or HVAC fan-only mode)",
        "  - A user overrode it via the thermostat app (fan_status='running (manual override)')",
        "  - It is a post-command thermostat transient (fan_status='running (untracked)')",
        "",
        "fan_status values explained:",
        "  inactive                  — fan is off; CA has no record of activating it",
        "  active                    — CA commanded the fan on (natural vent or HVAC fan-only)",
        "  running (manual override) — fan running; user overrode CA's command at the thermostat",
        "  running (untracked)       — thermostat reports fan on but CA's _fan_active=False;",
        "                             typical after HA restart, or post-heat blowdown transient",
        "  disabled                  — fan control feature is turned off in configuration",
        "",
        "Heating/cooling deadband (thermostat behavior — not a CA fault):",
        "  Thermostats have a built-in deadband. Heating fires when indoor drops ~1-2°F",
        "  below the setpoint and runs until slightly above. If CA commanded heat mode",
        "  at comfort_heat=68°F and indoor=67°F, the thermostat reporting hvac_action=idle",
        "  or hvac_action=fan is expected deadband behavior — not a CA failure.",
        "",
        "Warm-day comfort floor guard:",
        "  When day_type is warm/hot, CA sets hvac_mode=off — but ONLY after indoor reaches",
        "  comfort_heat. If indoor < comfort_heat at automation time, CA heats first",
        "  (event: warm_day_comfort_gap) then shuts off. A brief morning heating cycle on",
        "  a warm day is intentional. This guard prevents comfort violations at shutoff.",
        "",
        "Natural ventilation / economizer maintain phase:",
        "  CA can set hvac_mode=off AND fan_mode=on simultaneously for fan-only air",
        "  circulation. hvac_mode=off with fan running is NOT a contradiction when",
        "  fan_status=active or natural_vent_active=True. This is the economizer phase.",
        "",
        "State contradiction warning:",
        "  Fires when hvac_mode=off and hvac_action is heating/cooling/fan AND",
        "  the fan is not CA-controlled and not already classified as untracked.",
        "  It does NOT fire for untracked fans (already acknowledged) or CA-activated fans.",
        "",
    ]

    # Version and release notes (Issue #105)
    lines.append(_build_version_context(coordinator))

    # GitHub issues context (Issue #105)
    github_ctx = await async_build_github_context(coordinator.hass)
    if github_ctx:
        lines.append(github_ctx)

    return "\n".join(lines)


def parse_investigation_response(raw_text: str) -> dict[str, Any]:
    """Parse a Claude investigation response into a section dict.

    Splits on ## SECTION_NAME headers. Unrecognised headers are skipped.
    Missing sections default to empty string. The original raw text is
    always preserved in the 'full_text' key.
    """
    sections: dict[str, Any] = {
        "summary": "",
        "incongruities": "",
        "data_quality": "",
        "errors_warnings": "",
        "hypotheses": "",
        "recommended_actions": "",
        "assumptions": "",
        "full_text": raw_text,
    }

    _header_map = {
        "INVESTIGATION SUMMARY": "summary",
        "INCONGRUITIES FOUND": "incongruities",
        "DATA QUALITY ISSUES": "data_quality",
        "SYSTEM ERRORS / WARNINGS": "errors_warnings",
        "HYPOTHESES": "hypotheses",
        "RECOMMENDED ACTIONS": "recommended_actions",
        "ASSUMPTIONS & CONFIDENCE": "assumptions",
    }

    if not raw_text:
        return sections

    current_key: str | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        if current_key is not None:
            sections[current_key] = "\n".join(current_lines).strip()

    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            _flush()
            current_lines = []
            header_name = stripped[3:].strip().upper()
            current_key = _header_map.get(header_name)
            if current_key is None:
                _LOGGER.debug(
                    "Investigation response parser: unknown header '%s', skipping",
                    stripped,
                )
        else:
            if current_key is not None:
                current_lines.append(line)

    _flush()

    # Always restore full_text — _flush() cannot overwrite it because it is not in _header_map
    sections["full_text"] = raw_text
    return sections


def investigation_fallback(coordinator: Any, **kwargs: Any) -> dict[str, Any]:
    """Return a lightweight investigation dict from coordinator data without AI.

    Scans available data for obvious issues that can be detected deterministically.
    Returns a dict with the same keys as parse_investigation_response so callers
    can treat AI and fallback results uniformly.
    """
    errors_parts: list[str] = []
    incongruity_parts: list[str] = []
    data_quality_parts: list[str] = []
    summary_parts: list[str] = []

    # --- Event log: scan for error/warning entries ---
    try:
        event_log: list[Any] = getattr(coordinator, "_event_log", []) or []
        hours: int = int(kwargs.get("hours", 48))
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)
        for entry in event_log[-200:]:
            if not isinstance(entry, dict):
                continue
            etype = str(entry.get("type", ""))
            if "error" not in etype.lower() and "warning" not in etype.lower():
                continue
            raw_time = entry.get("time")
            if raw_time is not None:
                try:
                    if isinstance(raw_time, datetime.datetime):
                        event_dt = raw_time
                        if event_dt.tzinfo is None:
                            event_dt = event_dt.replace(tzinfo=datetime.UTC)
                    else:
                        event_dt = datetime.datetime.fromisoformat(str(raw_time))
                        if event_dt.tzinfo is None:
                            event_dt = event_dt.replace(tzinfo=datetime.UTC)
                    if event_dt < cutoff:
                        continue
                except ValueError:
                    pass
            errors_parts.append(f"[{entry.get('time', '?')}] type={etype}: {entry}")
    except Exception:
        _LOGGER.warning("investigator fallback: failed to scan event log")

    # --- Learning engine checks ---
    try:
        learning = coordinator.learning if hasattr(coordinator, "learning") else None
        if learning is not None:
            # Check daily records for opened-but-zero-compliance
            try:
                state_obj = getattr(learning, "_state", None)
                if state_obj is not None:
                    raw_records = getattr(state_obj, "records", None)
                    if isinstance(raw_records, list):
                        for rec in raw_records[-14:]:
                            if not isinstance(rec, dict):
                                continue
                            date_val = rec.get("date", "?")
                            # `window_compliance` does NOT exist on DailyRecord — it is
                            # only an aggregate in get_compliance_summary(). Use the two
                            # per-record fields that do exist: windows_recommended and
                            # windows_opened (True only when recommended AND opened).
                            _win_recommended = rec.get("windows_recommended", False)
                            _win_opened = rec.get("windows_opened", False)
                            if _win_recommended and not _win_opened:
                                incongruity_parts.append(
                                    f"Record {date_val}: windows_recommended=True"
                                    " but windows_opened=False (user did not open windows"
                                    " on a recommended day)"
                                )
            except Exception:
                _LOGGER.warning("investigator fallback: failed to check daily records")

            # Compliance summary cross-checks
            try:
                compliance: dict[str, Any] = learning.get_compliance_summary() or {}
                window_compliance = compliance.get("window_compliance")
                suggestions: list[Any] = learning.generate_suggestions() or []
                has_low_compliance_suggestion = any(
                    isinstance(s, dict) and s.get("suggestion_type") == "low_window_compliance" for s in suggestions
                )
                if window_compliance is not None and has_low_compliance_suggestion:
                    try:
                        if float(window_compliance) == 0.0:
                            incongruity_parts.append(
                                "window_compliance is 0.0 but a 'low_window_compliance'"
                                " suggestion exists — compliance counter may be zeroed incorrectly"
                            )
                    except (TypeError, ValueError):
                        pass

                # High override count check
                total_overrides = compliance.get("total_manual_overrides")
                if total_overrides is not None:
                    try:
                        if int(total_overrides) > 50:
                            data_quality_parts.append(
                                f"total_manual_overrides={total_overrides} is unusually high (>50)."
                                f" Verify that overrides are not being double-counted."
                            )
                    except (TypeError, ValueError):
                        pass
            except Exception:
                _LOGGER.warning("investigator fallback: failed compliance cross-check")

            # Override count from frequent_overrides suggestion
            try:
                suggestions_check: list[Any] = learning.generate_suggestions() or []
                for sug in suggestions_check:
                    if not isinstance(sug, dict):
                        continue
                    if sug.get("suggestion_type") == "frequent_overrides":
                        evidence = sug.get("evidence", {})
                        count = evidence.get("override_count", None) if isinstance(evidence, dict) else None
                        if count is not None:
                            try:
                                if int(count) > 50:
                                    data_quality_parts.append(
                                        f"frequent_overrides suggestion cites override_count={count}"
                                        f" which exceeds the suspicious threshold of 50."
                                    )
                            except (TypeError, ValueError):
                                pass
            except Exception:
                _LOGGER.warning("investigator fallback: failed override suggestion check")
    except Exception:
        _LOGGER.warning("investigator fallback: failed to access learning engine")

    # --- Build summary ---
    total_issues = len(errors_parts) + len(incongruity_parts) + len(data_quality_parts)
    if total_issues == 0:
        summary_parts.append(
            "Fallback scan found no obvious incongruities, data quality issues, or system errors."
            " AI analysis was unavailable — a full investigation requires the Claude API."
        )
    else:
        summary_parts.append(
            f"Fallback scan (no AI) found {total_issues} potential issue(s):"
            f" {len(errors_parts)} error/warning event(s),"
            f" {len(incongruity_parts)} incongruity(ies),"
            f" {len(data_quality_parts)} data quality issue(s)."
            f" AI analysis was unavailable for deep cross-source verification."
        )

    return {
        "summary": "\n".join(summary_parts),
        "incongruities": "\n".join(incongruity_parts) if incongruity_parts else "None detected.",
        "data_quality": "\n".join(data_quality_parts) if data_quality_parts else "None detected.",
        "errors_warnings": (
            "\n".join(errors_parts) if errors_parts else "No errors or warnings in the supplied window."
        ),
        "hypotheses": "AI unavailable — hypotheses require cross-source analysis by Claude.",
        "recommended_actions": "Restore AI connectivity and re-run the full investigator skill.",
        "assumptions": "Fallback only scans deterministic patterns; deep inference was not performed.",
        "full_text": "",
    }


def register_investigator_skill(registry: AISkillRegistry) -> None:
    """Create and register the investigator skill with the given registry."""
    skill = AISkillDefinition(
        name=_SKILL_NAME,
        description=(
            "Performs deep cross-source analysis to find incongruities, data quality issues,"
            " and system errors. Compares AI summaries against raw data. Returns a structured"
            " report with hypotheses, evidence citations, and recommended actions."
        ),
        system_prompt=_SYSTEM_PROMPT,
        context_builder=async_build_investigator_context,
        response_parser=parse_investigation_response,
        fallback=investigation_fallback,
        triggered_by="manual",
        config_key_model=CONF_AI_INVESTIGATOR_MODEL,
        config_key_max_tokens=CONF_AI_INVESTIGATOR_MAX_TOKENS,
        config_key_reasoning=CONF_AI_INVESTIGATOR_REASONING,
    )
    registry.register(skill)
