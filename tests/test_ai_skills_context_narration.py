"""Tests for narration-mode and default-investigation context scoping.

Issue #563: before that fix, the silent/scheduled narration path (never sets `focus`)
fell into `select()`'s "no focus = run everything" branch — the same audit-depth
context the on-demand Investigate button used. `narration=True` narrows the set to
priority <= 1.

Issue #920: the on-demand Investigate path's own default (no focus, no recognised
focus keyword, `deep=False`) now gets that *same* priority <= 1 narrowing, instead of
always running every provider — cutting default report noise. `deep=True` opts back
into the old "run everything" behavior. The `config` provider was reclassified from
priority 2 to priority 1 as part of this change (its comfort-band bounds are required
by the system prompt's NUMERIC VERIFICATION RULE and must be present by default).
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
    "config",
}

_DEEP_ONLY_NAMES = {
    "daily_summaries",
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
        assert names.isdisjoint(_DEEP_ONLY_NAMES)

    def test_default_investigation_now_matches_narration_scope(self):
        """Issue #920: the on-demand Investigate default (no focus, no deep) is now
        the same priority <= 1 set narration already used — not "run everything"."""
        registry = get_provider_registry()
        narrowed = registry.select(focus="")
        narration = registry.select(focus="", narration=True)

        assert {p.name for p in narrowed} == {p.name for p in narration}

    def test_deep_true_with_no_focus_returns_all_providers(self):
        """`deep=True` is the escape hatch that restores the old "audit everything"
        default-investigation behavior."""
        registry = get_provider_registry()
        deep = registry.select(focus="", deep=True)
        narrowed = registry.select(focus="")

        assert len(deep) > len(narrowed)
        assert _DEEP_ONLY_NAMES.issubset({p.name for p in deep})

    def test_narration_takes_precedence_even_with_recognised_focus_keyword(self):
        """narration=True should never expand back out via a focus tag match — narration
        call sites never set focus in practice, but the cutoff must hold regardless."""
        registry = get_provider_registry()
        selected = registry.select(focus="thermal", narration=True)
        assert {p.name for p in selected} == _NARRATION_EXPECTED_NAMES

    def test_unrecognised_focus_keyword_falls_back_to_default_depth_not_everything(self):
        """Issue #920: an unrecognised focus keyword used to fall back to "run
        everything"; it now falls back to the same default (non-deep) depth as an
        empty focus, and `deep=True` still restores the full set."""
        registry = get_provider_registry()
        narrowed = registry.select(focus="gibberish-not-a-real-keyword")
        deep = registry.select(focus="gibberish-not-a-real-keyword", deep=True)

        assert {p.name for p in narrowed} == _NARRATION_EXPECTED_NAMES
        assert len(deep) > len(narrowed)


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

    def test_non_deep_empty_focus_caps_at_priority_one(self):
        registry = self._build_registry()
        selected = registry.select(focus="")
        assert [p.name for p in selected] == ["p0", "p1"]

    def test_deep_empty_focus_returns_all_sorted_by_priority(self):
        registry = self._build_registry()
        selected = registry.select(focus="", deep=True)
        assert [p.name for p in selected] == ["p0", "p1", "p2", "p3", "p4"]
