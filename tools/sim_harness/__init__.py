"""sim_harness — headless execution infrastructure for AutomationEngine.

Provides FakeHass, FakeScheduler/virtual clock, and headless engine builder
so the production AutomationEngine can be driven deterministically without
a running Home Assistant instance.

Phase G1: infrastructure + smoke test only.
Phase G2 (future): scenario adapter.
Phase G3 (future): assertion layer.
"""
