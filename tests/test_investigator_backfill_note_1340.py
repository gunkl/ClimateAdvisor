"""Issue #1340: the Investigator must not tell the occupant to run a script their install does not have.

Occupant story: with no chart-log observations yet (a new install), the AI Investigator's report recommended
"python tools/thermal_replay.py --chart-log --write to backfill". That script is a developer tool in the repository;
a HACS install has no ``tools/`` folder, so the occupant was sent after a command they could not run. The fit is
automatic, so the context now says what to expect and that there is nothing to run.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from custom_components.climate_advisor.ai_skills_context import build_thermal_pipeline_context
from tests.test_ai_skills_context_learning_health import _make_coordinator


def _context(endpoint: int, block_ols: int) -> str:
    health = {"source_endpoint_count": endpoint, "source_block_ols_count": block_ols}
    return asyncio.run(build_thermal_pipeline_context(MagicMock(), _make_coordinator(health)))


def test_no_observations_explains_the_automatic_fit_and_names_no_script():
    ctx = _context(0, 0)
    note = next(line for line in ctx.splitlines() if "NOTE: 0 chart_log observations" in line)
    assert "tools/" not in note and "python" not in note and "--write" not in note
    assert "on its own" in note and "do not recommend running a script" in note


def test_control_with_observations_there_is_no_note():
    assert "NOTE: 0 chart_log observations" not in _context(3, 0)
