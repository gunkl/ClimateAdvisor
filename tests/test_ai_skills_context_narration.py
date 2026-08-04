"""Tests for narration-mode context scoping (Issue #563).

Before this fix, the silent/scheduled narration path (never sets `focus`) fell into
`select()`'s "no focus = run everything" branch — the same 16-provider audit-depth
context the on-demand Investigate button uses. These tests prove the `narration=True`
cutoff actually narrows the set to priority <= 1, and that the on-demand path
(narration=False, the default) is completely unaffected.
"""

from __future__ import annotations

from custom_components.climate_advisor.ai_skills_context import (
    ContextProvider,
    ContextProviderRegistry,
    get_provider_registry,
)

_NARRATION_EXPECTED_NAMES = {
    "current_state",
    "hvac_entity",
    "state_cross_validation",
    "last_briefing",
    "learning",
    "thermal_pipeline",
    "event_log",
    "activity_timeline",
    "override_details",
}

_INVESTIGATION_ONLY_NAMES = {
    "daily_summaries",
    "ai_report_history",
    "config",
    "operational_design",
    "known_fixes",
    "version",
    "github",
}


class TestNarrationScoping:
    def test_narration_true_returns_only_priority_le_1(self):
        registry = get_provider_registry()
        selected = registry.select(focus="", narration=True)
        names = {p.name for p in selected}

        assert names == _NARRATION_EXPECTED_NAMES
        assert all(p.priority <= 1 for p in selected)
        assert names.isdisjoint(_INVESTIGATION_ONLY_NAMES)

    def test_narration_false_with_no_focus_returns_all_providers(self):
        """Unchanged on-demand Investigate behavior — no focus means audit everything."""
        registry = get_provider_registry()
        unfiltered = registry.select(focus="")
        narrowed = registry.select(focus="", narration=False)

        assert len(narrowed) == len(unfiltered)
        assert {p.name for p in narrowed} == {p.name for p in unfiltered}
        # Sanity: this must be strictly larger than the narration set — the whole point
        # of this fix is that narration used to (wrongly) get this same full set.
        assert len(narrowed) > len(_NARRATION_EXPECTED_NAMES)

    def test_narration_takes_precedence_even_with_recognised_focus_keyword(self):
        """narration=True should never expand back out via a focus tag match — narration
        call sites never set focus in practice, but the cutoff must hold regardless."""
        registry = get_provider_registry()
        selected = registry.select(focus="thermal", narration=True)
        assert {p.name for p in selected} == _NARRATION_EXPECTED_NAMES


class TestSelectPriorityFilteringMechanics:
    """Isolated unit tests against a synthetic registry, independent of the real
    provider list, so this keeps passing even as providers are added/removed."""

    def _build_registry(self) -> ContextProviderRegistry:
        registry = ContextProviderRegistry()
        for name, priority in [("p0", 0), ("p1", 1), ("p2", 2), ("p3", 3), ("p4", 4)]:
            registry.register(ContextProvider(name=name, tags=frozenset(), priority=priority, builder=None))
        return registry

    def test_narration_caps_at_priority_one(self):
        registry = self._build_registry()
        selected = registry.select(focus="", narration=True)
        assert [p.name for p in selected] == ["p0", "p1"]

    def test_non_narration_empty_focus_returns_all_sorted_by_priority(self):
        registry = self._build_registry()
        selected = registry.select(focus="")
        assert [p.name for p in selected] == ["p0", "p1", "p2", "p3", "p4"]
