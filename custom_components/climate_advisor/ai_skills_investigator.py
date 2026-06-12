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
    OBS_TYPE_FAN_ONLY_DECAY,
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    OBS_TYPE_PASSIVE_DECAY,
    OBS_TYPE_SOLAR_GAIN,
    OBS_TYPE_VENTILATED_DECAY,
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
"within range" from proximity or narrative context â€” check the inequality directly
against the supplied numeric values. If you cannot verify the claim with the supplied
data, say "cannot verify" rather than guessing.

Always be explicit about the category of every claim you make:
- CONFIRMED FACT: the value is directly present in the supplied data
- INFERENCE: a conclusion deduced from a pattern across multiple data points
- ASSUMPTION: a guess made in the absence of direct evidence â€” always label these

INVESTIGATION PROCEDURE
1. Read all supplied data sections before drawing any conclusion.
2. Cross-check AI summary text against the raw numeric data. Where they disagree, flag the\
 discrepancy with exact values from both sources.
3. Scan for suspicious or impossible values, including but not limited to:
   - Window compliance reported as 0% on days when windows were physically opened
   - Override counts that are implausibly high (>50 in a short window)
   - Timestamps that are in the future, in the wrong timezone, or precede the system installation
   - Zeroed counters that should accumulate over time (runtime, observation counts)
   - Thermal rates (heating/cooling Â°F per hour) outside physically plausible bounds
   - Weather bias corrections that exceed the configured cap
4. Check the event log for any entries whose type contains "error" or "warning". Quote the\
 relevant event fields verbatim.
5. Generate 2â€“5 ranked hypotheses about what may be wrong or inconsistent. Rank by confidence\
 (highest first). Each hypothesis must cite at least one evidence item.
6. For every cited data value use the format: [source: <data_key>, value: <X>]
7. Where data is missing or unavailable, state explicitly: "Could not verify <X> â€” data not\
 present."
8. CROSS-CHECK AGAINST KNOWN-FIXED ISSUES: When an anomaly matches a pattern in the\
 KNOWN-FIXED ISSUES section, check whether the observed code path has a [COVERED] or\
 [NOT COVERED] marker. If [COVERED]: state "Issue #X fixed this path in vX.Y â€” treat as\
 resolved unless current data directly contradicts." If [NOT COVERED]: state "Issue #X\
 was scoped to path A; path B was explicitly not covered â€” candidate gap or incomplete fix."\
 When scope metadata is available, do not write "could not verify" â€” name the path and its\
 coverage status.
9. COUNT DISCREPANCY SUPPRESSION RULE: If `observation_count_heat` or `observation_count_cool`\
 in LEARNING â€” THERMAL MODEL differs from the corresponding pipeline committed count by exactly\
 1, this is consistent with EWMA flush lag (the model EWMA updates asynchronously after each\
 commit). Do NOT surface a gap of exactly 1 as an incongruity. Only flag if the gap exceeds 1\
 or if the same gap appears to have grown compared to a prior report.

OUTPUT FORMAT
SECTION ROLES ARE EXCLUSIVE â€” each section contains only what belongs to it:\
 do not repeat content already stated in a prior section.\
 A one-line cross-reference ("see Hypotheses above") is acceptable;\
 copying or paraphrasing the same analysis verbatim is not.
- INVESTIGATION SUMMARY: 3â€“5 sentence overview of the most significant finding and whether\
 action is required. No analysis detail, no hypothesis reasoning, no action items.
- INCONGRUITIES FOUND: Specific data mismatches or contradictions only. Do NOT re-explain\
 anything already stated in Summary.
- DATA QUALITY ISSUES: Missing data, sensor gaps, stale readings, unreliable values only.\
 Do NOT repeat incongruities.
- SYSTEM ERRORS / WARNINGS: Log errors and warnings verbatim (with counts if repeated).\
 Do NOT analyze causes â€” that belongs in Hypotheses.
- HYPOTHESES: Ranked explanations. Reference specific data from earlier sections by name\
 and value; do NOT restate the same findings verbatim.
- RECOMMENDED ACTIONS: Specific, actionable steps only. Do NOT re-state problem context â€”\
 just the action and which hypothesis or finding it addresses.
- ASSUMPTIONS & CONFIDENCE: List assumptions and confidence level only.\
 Do NOT repeat findings or recommendations.

Return your investigation using these exact section headers (## prefix, exact capitalisation):

## INVESTIGATION SUMMARY
3â€“5 sentence overview of the most important finding. If nothing is wrong, say so plainly\
 â€” do not fabricate issues.

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

THERMAL PIPELINE HEALTH rules:
- If hvac_heat or hvac_cool shows 0 committed observations AND HVAC has run: flag as observation\
 pipeline failure â€” expected to learn within first few cycles under normal conditions.
- If k_active_cool = NEVER LEARNED and AC has run in recent history: flag as pipeline failure;\
 suggest checking rejection log and pending observations for the hvac_cool type.
- If rejection log shows >=3 new_session_started abandonments for an HVAC type: flag as possible\
 short-cycling thermostat â€” HVAC cycles too short to capture post-heat samples between 5-min ticks.
- If rejection log shows n=0 rejections with delta_t=0.00Â°F: flag as possible sensor quantization\
 issue â€” thermostat reports 1Â°F resolution; suggest using a finer-grained sensor entity.
- If chart_log endpoint observations = 0: suggest running\
 python tools/thermal_replay.py --chart-log --write to backfill from historical data.
- Do NOT report k_active_cool=None as normal gap if AC has been running â€” it is a diagnostic flag\
 requiring investigation, not a routine "not yet learned" state.
- Source counts: "source_endpoint_count" and "source_block_ols_count" in the pipeline section\
 show how many observations came from the chart_log estimator vs online OLS. If both are 0,\
 no passive decay data has been committed at all.

ANOMALY RULE: SIMULTANEOUS AUTOMATION + OVERRIDE EVENTS (Issue #205)
If the thermal pipeline context or event log shows an `override_detected` event that occurs\
 within 60 seconds of an automation-initiated event (`nat_vent_*`, `ceiling_guard_fired`,\
 `classification_applied`, `grace_started` with source=automation), this is a false override\
 detection â€” automation actions must NEVER trigger override detection.

Classification: ACTIONABLE â€” false override detection (Bug #205)

Explanation: "An `override_detected` event at [time] followed/preceded by an automation event\
 at [time] (gap: Xs) indicates the override detection guard did not suppress the\
 automation-triggered thermostat state change. This is a code bug: the `_fan_command_pending`\
 or `_temp_command_pending` flag was not checked in the override detection guard.\
 Reference: Issue #205."

This should appear as a separate finding in the triage table under "Automation/Override Events"\
 regardless of whether the user mentions it.

TONE
Scientific, evidence-based, methodical. Prefer "no evidence of X" over "X is fine". Never\
 fabricate data or invent explanations â€” if the data does not support a conclusion, say so.\
"""


# Known automation cycle intervals (name → seconds).
# Used by _build_timing_correlations to flag manual events that coincide with
# automation-cycle boundaries. Extend this dict to add new intervals.
_AUTOMATION_INTERVALS_SECONDS: dict[str, int] = {
    "coordinator_cycle": 30 * 60,  # 30 min — main coordinator update cycle
    "manual_grace": 90 * 60,  # 90 min — manual override grace period
    "sensor_grace": 5 * 60,  # 5 min  — door/window sensor grace period
    "override_confirmation": 10 * 60,  # 10 min — override confirmation window
}

# How close a delta must be to a known interval to be flagged (seconds).
_TIMING_TOLERANCE_S: int = 2 * 60  # ±2 minutes

# Event types treated as automation-sourced for timing correlation purposes.
_TIMING_AUTO_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "classification_applied",
        "comfort_band_applied",
        "grace_started",
        "grace_expired",
        "nat_vent_started",
        "nat_vent_ended",
        "nat_vent_ceiling_escalation",
        "nat_vent_comfort_floor_exit",
        "nat_vent_predicted_floor_exit",
        "nat_vent_outdoor_rise_exit",
        "nat_vent_away_ceiling_exit",
        "ceiling_guard_fired",
        "warm_day_state_confirmed",
        "warm_day_setback_applied",
        "warm_day_comfort_gap",
        "occupancy_setback",
        "occupancy_comfort_restored",
        "morning_wakeup",
    }
)

# Event types treated as manual-sourced for timing correlation purposes.
_TIMING_MANUAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "override_detected",
        "override_confirmed",
        "override_cleared",
        "override_self_resolved",
        "manual_override_cleared",
        "handle_manual_override",
        "handle_fan_manual_override",
    }
)


def _build_timing_correlations(events: list) -> str:
    """Build a TIMING CORRELATIONS section for the investigator context.

    Scans the event log for manual events that occur within ±2 minutes of a
    known automation interval after an automation event. These coincidences
    suggest the "manual" event may actually be automation-caused.

    Returns a formatted string starting with '=== TIMING CORRELATIONS ==='.
    """
    import datetime as _dt

    lines: list[str] = ["=== TIMING CORRELATIONS ==="]
    if not events:
        lines.append("  (no events to correlate)")
        return "\n".join(lines)

    # Resolve timestamps to UTC datetime objects
    resolved: list[tuple] = []  # (dt | None, event_dict)
    for entry in events:
        if not isinstance(entry, dict):
            continue
        raw_time = entry.get("time")
        event_dt = None
        if isinstance(raw_time, _dt.datetime):
            event_dt = raw_time
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=_dt.UTC)
        elif raw_time is not None:
            try:
                event_dt = _dt.datetime.fromisoformat(str(raw_time))
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=_dt.UTC)
            except ValueError:
                pass
        resolved.append((event_dt, entry))

    # Collect automation events (with parseable timestamps)
    auto_events = [
        (dt, e)
        for dt, e in resolved
        if dt is not None and (e.get("source") in ("automation",) or str(e.get("type", "")) in _TIMING_AUTO_EVENT_TYPES)
    ]

    # Check each manual event against all prior automation events
    found_any = False
    for evt_dt, evt in resolved:
        etype = str(evt.get("type", ""))
        is_manual = evt.get("source") == "manual" or etype in _TIMING_MANUAL_EVENT_TYPES
        if not is_manual or evt_dt is None:
            continue

        # Find the nearest prior automation event
        prior_auto = [(adt, ae) for adt, ae in auto_events if adt < evt_dt]
        if not prior_auto:
            lines.append(f"  [OK] {evt_dt.strftime('%H:%M')} — {etype}: no prior automation event in window")
            found_any = True
            continue

        # Most recent prior automation event
        nearest_adt, nearest_ae = max(prior_auto, key=lambda x: x[0])
        delta_s = (evt_dt - nearest_adt).total_seconds()
        time_str = evt_dt.strftime("%H:%M")
        prior_type = str(nearest_ae.get("type", "?"))
        prior_time_str = nearest_adt.strftime("%H:%M")

        # Check against known intervals
        matched_interval: str | None = None
        for interval_name, interval_s in _AUTOMATION_INTERVALS_SECONDS.items():
            if abs(delta_s - interval_s) <= _TIMING_TOLERANCE_S:
                matched_interval = interval_name
                break

        if matched_interval is not None:
            delta_min = delta_s / 60
            lines.append(
                f"  [TIMING-COINCIDENT] {time_str} — {etype}: "
                f"{delta_min:.0f}m after {prior_type} at {prior_time_str} "
                f"(\u2248{matched_interval.replace('_', '-')}) — may be automation-caused"
            )
        else:
            lines.append(
                f"  [OK] {time_str} — {etype}: no matching automation interval (delta={delta_s:.0f}s from {prior_type})"
            )
        found_any = True

    if not found_any:
        lines.append("  (no manual events in window)")

    return "\n".join(lines)


def _fmt_window_compliance(compliance: dict) -> str:
    """Format window_compliance with its denominator for unambiguous AI interpretation.

    Produces e.g. "0.6667 (2 of 3 windows-recommended days)" so the AI cannot
    mistake the denominator for the total recording window.
    """
    val = compliance.get("window_compliance")
    denom = compliance.get("window_compliance_denominator", 0)
    if val is None:
        return "none (no windows-recommended days in window)"
    if denom == 0:
        return f"{val} (denominator=0)"
    numerator = round(val * denom)
    return f"{val:.4f} ({numerator} of {denom} windows-recommended days)"


def _build_thermal_pipeline_context(coordinator) -> str:
    """Build THERMAL OBSERVATION PIPELINE section for the investigator context.

    Calls coordinator._build_learning_health() and coordinator._build_thermal_pipeline_summary()
    to surface per-obs-type rejection counts, pending observation state, and engine status so the
    AI can distinguish 'k_active_cool=None because never learned' from 'pipeline failure'.

    Each obs_type row shows:
      committed / total_attempts, top rejection reason, NEVER LEARNED flag if k_active_* is None.
    Pending observations show phase, elapsed time, and sample count.
    """
    from .ai_skills_activity import _format_engine_status_for_ai  # noqa: PLC0415

    lines: list[str] = ["=== THERMAL OBSERVATION PIPELINE ==="]

    # --- Per-type health from _build_learning_health() ---
    try:
        health: dict = (
            coordinator._build_learning_health()
            if callable(getattr(coordinator, "_build_learning_health", None))
            else {}
        )
    except Exception:
        health = {}

    # Retrieve current thermal model so we can flag NEVER LEARNED parameters
    try:
        learning = getattr(coordinator, "learning", None)
        thermal: dict = (learning.get_thermal_model() if learning is not None else {}) or {}
    except Exception:
        thermal = {}

    k_active_cool = thermal.get("k_active_cool")
    k_active_heat = thermal.get("k_active_heat")

    all_obs_types = [
        OBS_TYPE_HVAC_HEAT,
        OBS_TYPE_HVAC_COOL,
        OBS_TYPE_PASSIVE_DECAY,
        OBS_TYPE_FAN_ONLY_DECAY,
        OBS_TYPE_VENTILATED_DECAY,
        OBS_TYPE_SOLAR_GAIN,
    ]

    # Reason codes that indicate the observation was interrupted by normal system operation.
    # These are expected on active days; a high count is NOT a quality failure.
    # All operational interruptions are stored under the "abandoned" reason_code in the
    # rejection log (the operational sub-reason is captured in the log message but not
    # persisted separately in the health dict).
    _OPERATIONAL_CODES = {"abandoned"}

    # Reason codes that indicate a signal quality problem worth flagging.
    _QUALITY_FAILURE_CODES = {
        "too_few_samples",
        "too_few_blocks",
        "small_delta",
        "ols_bad_fit",
        "ols_wrong_sign",
        "ols_bounds",
        "window_too_short",
        "no_interior_peak",
    }

    lines.append("Per-type rejection summary:")
    hvac_heat_committed = 0
    hvac_cool_committed = 0
    hvac_heat_total_rejected = 0
    hvac_cool_total_rejected = 0

    for obs_type in all_obs_types:
        type_health = health.get(obs_type, {})
        committed = type_health.get("committed", 0)
        rejections_by_code: dict = type_health.get("rejections", {})
        total_rejected = sum(rejections_by_code.values())

        # Split rejections into operational interruptions vs quality failures
        operational_count = sum(rejections_by_code.get(rc, 0) for rc in _OPERATIONAL_CODES)
        quality_failures: dict[str, int] = {
            rc: cnt for rc, cnt in rejections_by_code.items() if rc in _QUALITY_FAILURE_CODES and cnt > 0
        }
        quality_count = sum(quality_failures.values())

        # Track HVAC totals for pipeline failure detection
        if obs_type == OBS_TYPE_HVAC_HEAT:
            hvac_heat_committed = committed
            hvac_heat_total_rejected = total_rejected
        elif obs_type == OBS_TYPE_HVAC_COOL:
            hvac_cool_committed = committed
            hvac_cool_total_rejected = total_rejected

        # Build suffix markers
        suffix_parts: list[str] = []
        if obs_type == OBS_TYPE_HVAC_COOL and k_active_cool is None:
            suffix_parts.append("NEVER LEARNED â€” k_active_cool is None")
        if obs_type == OBS_TYPE_HVAC_HEAT and k_active_heat is None:
            suffix_parts.append("NEVER LEARNED â€” k_active_heat is None")
        suffix = f"  [{', '.join(suffix_parts)}]" if suffix_parts else ""

        lines.append(f"  {obs_type}: {committed} committed, {total_rejected} rejected{suffix}")
        if total_rejected == 0:
            lines.append("    â€” no rejections")
        else:
            if operational_count > 0:
                lines.append(f"    â€” operational interruptions: {operational_count} [expected on active days]")
            if quality_count > 0:
                qf_parts = ", ".join(
                    f"{rc} x{cnt}" for rc, cnt in sorted(quality_failures.items(), key=lambda x: -x[1])
                )
                lines.append(f"    â€” quality failures: {quality_count} ({qf_parts})")
            elif total_rejected > 0:
                lines.append("    â€” no quality failures")

    # Pipeline failure detection
    hvac_total_committed = hvac_heat_committed + hvac_cool_committed
    hvac_total_rejected = hvac_heat_total_rejected + hvac_cool_total_rejected
    if hvac_total_committed == 0 and hvac_total_rejected > 0:
        lines.append(
            f"  *** PIPELINE FAILURE INDICATOR: 0 committed HVAC observations,"
            f" {hvac_total_rejected} rejections â€” pipeline is not learning from HVAC cycles ***"
        )

    # Source estimator counts
    endpoint_count = health.get("source_endpoint_count", 0)
    block_ols_count = health.get("source_block_ols_count", 0)
    lines.append(f"  chart_log endpoint observations: {endpoint_count}")
    lines.append(f"  block-OLS observations: {block_ols_count}")
    if endpoint_count == 0 and block_ols_count == 0:
        lines.append(
            "  NOTE: 0 chart_log observations â€” consider running"
            " python tools/thermal_replay.py --chart-log --write to backfill"
        )

    # --- Engine status ---
    lines.append("")
    lines.append("Engine status:")
    try:
        if learning is not None and hasattr(learning, "get_engine_status"):
            engine_status = learning.get_engine_status()
            from .ai_skills_activity import _format_engine_status_for_ai  # noqa: PLC0415, F811

            engine_lines = _format_engine_status_for_ai(engine_status)
            lines.append(engine_lines)
        else:
            lines.append("  unavailable")
    except Exception:
        lines.append("  unavailable")

    lines.append("")
    return "\n".join(lines)


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


def _build_known_fixes_context(coordinator) -> str:
    """Inject KNOWN_FIXES behavioral invariant registry into investigator context.

    Provides scope boundaries so the analyzer can state '[COVERED] â€” resolved'
    or '[NOT COVERED] â€” potential gap' rather than hedging 'could not verify.'
    """
    from .const import KNOWN_FIXES  # noqa: PLC0415

    if not KNOWN_FIXES:
        return ""
    lines = ["## KNOWN-FIXED ISSUES (scope-bounded â€” use for cross-check, step 8)"]
    for issue_num in sorted(KNOWN_FIXES.keys(), reverse=True):
        fix = KNOWN_FIXES[issue_num]
        lines.append(f"\nIssue #{issue_num} â€” fixed in v{fix['version_fixed']}: {fix['title']}")
        for covered in fix.get("scope_covered", []):
            lines.append(f"  [COVERED] {covered}")
        for gap in fix.get("scope_not_covered", []):
            lines.append(f"  [NOT COVERED] {gap}")
    lines.append("")
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

    # Time window â€” controls event log cutoff and daily records lookback
    hours: int = min(max(int(kwargs.get("hours", 168)), 1), 720)
    daily_records_days: int = min((hours + 23) // 24 + 1, 30)

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
        # Compute fresh runtime â€” coordinator.data may be up to 30 min stale
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
        _LOGGER.warning("investigator: failed to read coordinator.data â€” skipping current state")
        lines += ["=== CURRENT STATE ===", "  unavailable", ""]

    # ------------------------------------------------------------------
    # 2. HVAC entity state from HA
    # ------------------------------------------------------------------
    try:
        climate_entity_id: str = (coordinator.config or {}).get("climate_entity", "")
        hvac_mode = "unknown"
        current_temp = "unknown"
        target_temp_low = "unknown"
        target_temp_high = "unknown"
        if climate_entity_id:
            climate_state = hass.states.get(climate_entity_id)
            if climate_state is not None:
                hvac_mode = climate_state.state
                current_temp = climate_state.attributes.get("current_temperature", "unknown")
                target_temp_low = climate_state.attributes.get("target_temp_low", "unknown")
                target_temp_high = climate_state.attributes.get("target_temp_high", "unknown")

        lines += [
            "=== HVAC ENTITY ===",
            f"  entity_id:        {climate_entity_id or 'not configured'}",
            f"  hvac_mode:        {hvac_mode}",
            f"  current_temp:     {current_temp}",
            f"  target_temp_low:  {target_temp_low}",
            f"  target_temp_high: {target_temp_high}",
            "",
        ]
    except Exception:
        _LOGGER.warning("investigator: failed to read HVAC entity state â€” skipping")
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
                    "=== LEARNING â€” COMPLIANCE SUMMARY ===",
                    f"  window_compliance:              {_fmt_window_compliance(compliance)}",
                    f"  avg_daily_hvac_runtime_minutes: {compliance.get('avg_daily_hvac_runtime_minutes', 'unknown')}",
                    f"  comfort_score:                  {compliance.get('comfort_score', 'unknown')}",
                    f"  total_manual_overrides:         {compliance.get('total_manual_overrides', 'unknown')}",
                    f"  pending_suggestions:            {compliance.get('pending_suggestions', 'unknown')}",
                    "  NOTE â€” window_compliance scope: the value above uses the last 14 days only",
                    "  (get_compliance_summary() 14-day window). The suggestion engine uses full",
                    "  historical records. A discrepancy between compliance summary and suggestion",
                    "  engine values is expected when non-compliant days exist outside the 14-day",
                    "  window â€” this is not a calculation bug.",
                    "",
                ]
            except Exception:
                _LOGGER.warning("investigator: get_compliance_summary() failed")
                lines += ["=== LEARNING â€” COMPLIANCE SUMMARY ===", "  unavailable", ""]

            # Thermal model
            try:
                thermal: dict[str, Any] = learning.get_thermal_model() or {}
                lines += [
                    "=== LEARNING â€” THERMAL MODEL ===",
                    f"  heating_rate_f_per_hour:   {thermal.get('heating_rate_f_per_hour', 'unknown')}",
                    f"  cooling_rate_f_per_hour:   {thermal.get('cooling_rate_f_per_hour', 'unknown')}",
                    f"  confidence:                {thermal.get('confidence', 'unknown')}",
                    f"  observation_count_heat:    {thermal.get('observation_count_heat', 'unknown')}",
                    f"  observation_count_cool:    {thermal.get('observation_count_cool', 'unknown')}",
                    "",
                ]
            except Exception:
                _LOGGER.warning("investigator: get_thermal_model() failed")
                lines += ["=== LEARNING â€” THERMAL MODEL ===", "  unavailable", ""]

            # Weather bias
            try:
                bias: dict[str, Any] = learning.get_weather_bias() or {}
                lines += [
                    "=== LEARNING â€” WEATHER BIAS ===",
                    f"  high_bias:          {bias.get('high_bias', 'unknown')}",
                    f"  low_bias:           {bias.get('low_bias', 'unknown')}",
                    f"  confidence:         {bias.get('confidence', 'unknown')}",
                    f"  observation_count:  {bias.get('observation_count', 'unknown')}",
                    "",
                ]
            except Exception:
                _LOGGER.warning("investigator: get_weather_bias() failed")
                lines += ["=== LEARNING â€” WEATHER BIAS ===", "  unavailable", ""]

            # Active suggestions
            try:
                suggestions: list[Any] = learning.generate_suggestions() or []
                lines.append("=== LEARNING â€” ACTIVE SUGGESTIONS ===")
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
                lines += ["=== LEARNING â€” ACTIVE SUGGESTIONS ===", "  unavailable", ""]

            # Daily records â€” window determined by caller's hours parameter
            try:
                state_obj = getattr(learning, "_state", None)
                records: list[Any] = []
                if state_obj is not None:
                    raw_records = getattr(state_obj, "records", None)
                    if isinstance(raw_records, list):
                        records = raw_records[-daily_records_days:]

                lines.append(f"=== LEARNING â€” LAST {daily_records_days} DAILY RECORDS ===")
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
                lines += [f"=== LEARNING â€” LAST {daily_records_days} DAILY RECORDS ===", "  unavailable", ""]
        else:
            lines += ["=== LEARNING ===", "  learning engine not available", ""]
    except Exception:
        _LOGGER.warning("investigator: failed to access learning engine â€” skipping")
        lines += ["=== LEARNING ===", "  unavailable", ""]

    # ------------------------------------------------------------------
    # 3b. Thermal observation pipeline health (Issue #156)
    # ------------------------------------------------------------------
    try:
        lines.append(_build_thermal_pipeline_context(coordinator))
    except Exception:
        _LOGGER.warning("investigator: failed to build thermal pipeline context â€” skipping")
        lines += ["=== THERMAL OBSERVATION PIPELINE ===", "  unavailable", ""]

    # ------------------------------------------------------------------
    # 4. Event log
    # ------------------------------------------------------------------
    try:
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
        _LOGGER.warning("investigator: failed to read event log â€” skipping")
        lines += ["=== EVENT LOG ===", "  unavailable", ""]

    # ------------------------------------------------------------------
    # 4b. Timing correlation analysis
    # ------------------------------------------------------------------
    try:
        raw_log: list[Any] = getattr(coordinator, "_event_log", []) or []
        lines.append(_build_timing_correlations(raw_log))
        lines.append("")
    except Exception:
        _LOGGER.warning("investigator: failed to build timing correlations -- skipping")
        lines += ["=== TIMING CORRELATIONS ===", "  unavailable", ""]

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
        _LOGGER.warning("investigator: failed to read AI report history â€” skipping")
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
            f"  comfort_heat (lower bound): {_comfort_heat} â€” indoor must be >= this to be in comfort band",
            f"  comfort_cool (upper bound): {_comfort_cool} â€” indoor must be <= this to be in comfort band",
            f"  comfort_band: [{_comfort_heat}, {_comfort_cool}]Â°F"
            " â€” temperature T is in-band only if comfort_heat <= T <= comfort_cool",
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
        _LOGGER.warning("investigator: failed to read config â€” skipping")
        lines += ["=== CONFIGURATION ===", "  unavailable", ""]

    # ------------------------------------------------------------------
    # 7. CA operational design â€” prevents the AI from hallucinating
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
        "  inactive                  â€” fan is off; CA has no record of activating it",
        "  active                    â€” CA commanded the fan on (natural vent or HVAC fan-only)",
        "  running (manual override) â€” fan running; user overrode CA's command at the thermostat",
        "  running (untracked)       â€” thermostat reports fan on but CA's _fan_active=False;",
        "                             typical after HA restart, or post-heat blowdown transient",
        "  off (manual override)     â€” _fan_override_active=True AND _fan_active=False; user turned",
        "                             the fan on at the thermostat (setting _fan_override_active=True),",
        "                             then turned it off before the grace period expired. The override",
        "                             is still in effect (grace period not yet cleared), physical fan is off.",
        "  disabled                  â€” fan control feature is turned off in configuration",
        "",
        "Heating/cooling deadband (thermostat behavior â€” not a CA fault):",
        "  Thermostats have a built-in deadband. Heating fires when indoor drops ~1-2Â°F",
        "  below the setpoint and runs until slightly above. If CA commanded heat mode",
        "  at comfort_heat=68Â°F and indoor=67Â°F, the thermostat reporting hvac_action=idle",
        "  or hvac_action=fan is expected deadband behavior â€” not a CA failure.",
        "",
        "Warm-day comfort floor guard:",
        "  When day_type is warm/hot, CA sets hvac_mode=off â€” but ONLY after indoor reaches",
        "  comfort_heat. If indoor < comfort_heat at automation time, CA heats first",
        "  (event: warm_day_comfort_gap) then shuts off. A brief morning heating cycle on",
        "  a warm day is intentional. This guard prevents comfort violations at shutoff.",
        "The warm_day_state_confirmed event fires every 30 min when the thermostat is already off"
        " (heartbeat) — no service call is made.",
        "The warm_day_setback_applied event fires when an actual setpoint or mode change is needed"
        " (cool→setback_cool, heat→setback_heat, or hard off).",
        "High event counts for warm_day_state_confirmed on sustained warm days are expected normal"
        " behavior — 60+ firings in 48 hours is typical.",
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

    # Behavioral invariant registry â€” scope-bounded fix history (Issue #144)
    lines.append(_build_known_fixes_context(coordinator))

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

    # Always restore full_text â€” _flush() cannot overwrite it because it is not in _header_map
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
                        for rec in raw_records[-30:]:
                            if not isinstance(rec, dict):
                                continue
                            date_val = rec.get("date", "?")
                            # `window_compliance` does NOT exist on DailyRecord â€” it is
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
                                " suggestion exists â€” compliance counter may be zeroed incorrectly"
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
            " AI analysis was unavailable â€” a full investigation requires the Claude API."
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
        "hypotheses": "AI unavailable â€” hypotheses require cross-source analysis by Claude.",
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
