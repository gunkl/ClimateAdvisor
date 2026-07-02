"""Investigative Agent AI skill for Climate Advisor (Issue #82)."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


from .ai_skills import AISkillDefinition, AISkillRegistry
from .ai_skills_context import (
    _AUTOMATION_INTERVALS_SECONDS,
    _build_timing_correlations,
    _fetch_github_issues,
)
from .ai_skills_context import (
    build_version_context as _build_version_context_async,
)
from .const import (
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
"within range" from proximity or narrative context â€" check the inequality directly
against the supplied numeric values. If you cannot verify the claim with the supplied
data, say "cannot verify" rather than guessing.

Always be explicit about the category of every claim you make:
- CONFIRMED FACT: the value is directly present in the supplied data
- INFERENCE: a conclusion deduced from a pattern across multiple data points
- ASSUMPTION: a guess made in the absence of direct evidence â€" always label these

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
5. Generate 2â€"5 ranked hypotheses about what may be wrong or inconsistent. Rank by confidence\
 (highest first). Each hypothesis must cite at least one evidence item.
6. For every cited data value use the format: [source: <data_key>, value: <X>]
7. Where data is missing or unavailable, state explicitly: "Could not verify <X> â€" data not\
 present."
8. CROSS-CHECK AGAINST KNOWN-FIXED ISSUES: When an anomaly matches a pattern in the\
 KNOWN-FIXED ISSUES section, check whether the observed code path has a [COVERED] or\
 [NOT COVERED] marker. If [COVERED]: state "Issue #X fixed this path in vX.Y â€" treat as\
 resolved unless current data directly contradicts." If [NOT COVERED]: state "Issue #X\
 was scoped to path A; path B was explicitly not covered â€" candidate gap or incomplete fix."\
 When scope metadata is available, do not write "could not verify" â€" name the path and its\
 coverage status.
9. COUNT DISCREPANCY SUPPRESSION RULE: If `observation_count_heat` or `observation_count_cool`\
 in LEARNING â€" THERMAL MODEL differs from the corresponding pipeline committed count by exactly\
 1, this is consistent with EWMA flush lag (the model EWMA updates asynchronously after each\
 commit). Do NOT surface a gap of exactly 1 as an incongruity. Only flag if the gap exceeds 1\
 or if the same gap appears to have grown compared to a prior report.

OUTPUT FORMAT
SECTION ROLES ARE EXCLUSIVE â€" each section contains only what belongs to it:\
 do not repeat content already stated in a prior section.\
 A one-line cross-reference ("see Hypotheses above") is acceptable;\
 copying or paraphrasing the same analysis verbatim is not.
- INVESTIGATION SUMMARY: 3â€"5 sentence overview of the most significant finding and whether\
 action is required. No analysis detail, no hypothesis reasoning, no action items.
- INCONGRUITIES FOUND: Specific data mismatches or contradictions only. Do NOT re-explain\
 anything already stated in Summary.
- DATA QUALITY ISSUES: Missing data, sensor gaps, stale readings, unreliable values only.\
 Do NOT repeat incongruities.
- SYSTEM ERRORS / WARNINGS: Log errors and warnings verbatim (with counts if repeated).\
 Do NOT analyze causes â€" that belongs in Hypotheses.
- HYPOTHESES: Ranked explanations. Reference specific data from earlier sections by name\
 and value; do NOT restate the same findings verbatim.
- RECOMMENDED ACTIONS: Specific, actionable steps only. Do NOT re-state problem context â€"\
 just the action and which hypothesis or finding it addresses.
- ASSUMPTIONS & CONFIDENCE: List assumptions and confidence level only.\
 Do NOT repeat findings or recommendations.

Return your investigation using these exact section headers (## prefix, exact capitalisation):

## INVESTIGATION SUMMARY
3â€"5 sentence overview of the most important finding. If nothing is wrong, say so plainly\
 â€" do not fabricate issues.

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
 pipeline failure â€" expected to learn within first few cycles under normal conditions.
- If k_active_cool = NEVER LEARNED and AC has run in recent history: flag as pipeline failure;\
 suggest checking rejection log and pending observations for the hvac_cool type.
- If rejection log shows >=3 new_session_started abandonments for an HVAC type: flag as possible\
 short-cycling thermostat â€" HVAC cycles too short to capture post-heat samples between 5-min ticks.
- If rejection log shows n=0 rejections with delta_t=0.00Â°F: flag as possible sensor quantization\
 issue â€" thermostat reports 1Â°F resolution; suggest using a finer-grained sensor entity.
- If chart_log endpoint observations = 0: suggest running\
 python tools/thermal_replay.py --chart-log --write to backfill from historical data.
- Do NOT report k_active_cool=None as normal gap if AC has been running â€" it is a diagnostic flag\
 requiring investigation, not a routine "not yet learned" state.
- Source counts: "source_endpoint_count" and "source_block_ols_count" in the pipeline section\
 show how many observations came from the chart_log estimator vs online OLS. If both are 0,\
 no passive decay data has been committed at all.

ANOMALY RULE: SIMULTANEOUS AUTOMATION + OVERRIDE EVENTS (Issue #205)
If the thermal pipeline context or event log shows an `override_detected` event that occurs\
 within 60 seconds of an automation-initiated event (`nat_vent_*`, `ceiling_guard_fired`,\
 `classification_applied`, `grace_started` with source=automation), this is a false override\
 detection â€" automation actions must NEVER trigger override detection.

Classification: ACTIONABLE â€" false override detection (Bug #205)

Explanation: "An `override_detected` event at [time] followed/preceded by an automation event\
 at [time] (gap: Xs) indicates the override detection guard did not suppress the\
 automation-triggered thermostat state change. This is a code bug: the `_fan_command_pending`\
 or `_temp_command_pending` flag was not checked in the override detection guard.\
 Reference: Issue #205."

This should appear as a separate finding in the triage table under "Automation/Override Events"\
 regardless of whether the user mentions it.

TONE
Scientific, evidence-based, methodical. Prefer "no evidence of X" over "X is fine". Never\
 fabricate data or invent explanations â€" if the data does not support a conclusion, say so.\
"""


async def async_build_investigator_context(
    hass: HomeAssistant,
    coordinator: Any,
    **kwargs: Any,
) -> str:
    """Build context string by running all registered context providers in priority order."""
    from .ai_skills_context import get_provider_registry  # noqa: PLC0415

    registry = get_provider_registry()
    focus: str = kwargs.get("focus", "")
    providers = registry.select(focus)

    sections: list[str] = ["=== Climate Advisor Investigator Context ===", ""]

    # Prepend focus question if provided
    if focus:
        sections += [
            "=== INVESTIGATION FOCUS (USER-DIRECTED) ===",
            f"  {focus}",
            "",
        ]

    for provider in providers:
        try:
            section = await provider.builder(hass, coordinator, **kwargs)
            if section:
                sections.append(section)
        except Exception:
            _LOGGER.warning(
                "investigator: provider '%s' failed — skipping",
                provider.name,
                exc_info=True,
            )
            sections.append(f"=== {provider.name.upper()} ===\n  unavailable\n")

    return "\n".join(sections)


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

    # Always restore full_text â€" _flush() cannot overwrite it because it is not in _header_map
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
                            # `window_compliance` does NOT exist on DailyRecord â€" it is
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


# ---------------------------------------------------------------------------
# Backward-compat re-exports for tests that import from this module
# ---------------------------------------------------------------------------

# Re-export timing correlations and constants so tests can import them here.
# (Moved to ai_skills_context.py in the Phase 1 refactor.)
__all__ = [
    "_build_timing_correlations",
    "_AUTOMATION_INTERVALS_SECONDS",
]


def _build_version_context(coordinator: Any) -> str:
    """Sync compat shim for tests. Calls build_version_context(None, coordinator)."""
    import asyncio  # noqa: PLC0415

    return asyncio.run(_build_version_context_async(None, coordinator))


async def async_build_github_context(hass: Any) -> str:
    """Async compat shim: single-arg form used by tests. Delegates to _fetch_github_issues."""
    return await _fetch_github_issues(hass)


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
