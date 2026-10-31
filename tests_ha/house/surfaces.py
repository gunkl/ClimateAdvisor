"""Readers for what the occupant actually reads (Step 2 Increment 4, Issue #945).

Every value comes from the real surface, produced by the real coordinator inside real Home Assistant: the
Climate Advisor sensors and the notifications delivered to the notify service. Nothing here formats text.
"""

from __future__ import annotations

from typing import Any

from .builder import House

# Words the Next Automation card must never contain (Status Card Ontology, CLAUDE.md; enforced for the
# unit tier by tests/test_status_card_ontology.py). Checked beside the snapshot so a snapshot can never
# lock in a violation.
NEXT_AUTOMATION_BANNED_WORDS = ("window", "waiting", "paused", "grace")


def notifications(house: House) -> list[dict[str, str]]:
    return [{"title": c.data.get("title", ""), "message": c.data.get("message", "")} for c in house.notifications]


def read_surfaces(house: House) -> dict[str, Any]:
    """The occupant-facing strings at this moment, one entry per surface."""
    briefing = house.sensor("daily_briefing")
    nxt = f"{house.sensor('next_automation_action').state} @ {house.sensor('next_automation_time').state}"
    return {
        "status": house.sensor("automation_status").state,
        "next_user_action": house.sensor("next_human_action").state,
        "next_automation": nxt,
        "fan": house.sensor("fan_status").state,
        "briefing_tldr": briefing.attributes.get("tldr", ""),
        "briefing_full": briefing.attributes.get("full_briefing", ""),
        "notifications": notifications(house),
    }
