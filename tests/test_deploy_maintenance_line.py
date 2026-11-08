"""The 0.8.x maintenance line's deploy tool refuses a real deploy (owner decision 2026-11-08).

The owner's server runs the current line from main (0.9). Deploying this branch would roll the house back to 0.8,
possibly across storage changes, so tools/deploy.py on 0.8.x refuses unless --allow-maintenance-branch is given.
"""

from __future__ import annotations

import os
import sys

_TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import deploy  # noqa: E402


def test_a_deploy_from_the_maintenance_line_is_refused_by_default() -> None:
    ok, message = deploy.check_maintenance_line(False)
    assert not ok
    assert "roll the house back to 0.8" in message
    assert "--allow-maintenance-branch" in message


def test_the_override_allows_it_and_says_what_it_does() -> None:
    ok, message = deploy.check_maintenance_line(True)
    assert ok
    assert "rolled back to 0.8" in message
