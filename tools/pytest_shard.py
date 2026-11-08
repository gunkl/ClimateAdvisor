"""Round-robin test slices for parallel CI jobs: ``--ca-shard K/N`` keeps every Nth collected test from the Kth.

Shared by both test tiers (``tests/conftest.py`` and ``tests_ha/conftest.py`` call these from their own hooks).
``scripts/test.py`` turns ``CA_UNIT_SHARD`` / ``CA_HA_SHARD`` into the option.

Dealing tests round-robin in collection order spreads each slow file (``test_sim_generator``, the real-HA goldens)
over every slice instead of loading one slice with it, and needs no stored durations to keep in sync. Collection
order is deterministic, so the N slices partition the tier exactly, and xdist workers inherit the option, so every
worker of one slice selects the same tests. An option, not an environment variable: tests that start a nested
pytest run (``test_promise_skip_guard``) inherit the environment and would slice that run too.

Standard library and pytest only: imported by both tiers' environments.
"""

from __future__ import annotations

import pytest


def add_shard_option(parser: pytest.Parser) -> None:
    parser.addoption("--ca-shard", default=None, metavar="K/N", help="run only the Kth of N round-robin slices")


def _shard(config: pytest.Config) -> tuple[int, int] | None:
    raw = (config.getoption("--ca-shard") or "").strip()
    if not raw:
        return None
    k, _, n = raw.partition("/")
    try:
        shard, total = int(k), int(n)
    except ValueError:
        shard, total = 0, 0
    if not 1 <= shard <= total:
        raise pytest.UsageError(f"--ca-shard must be K/N with 1 <= K <= N, got {raw!r}")
    return shard, total


def apply_shard(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep only this slice's items (call before any reordering, so the slices stay a partition)."""
    if (spec := _shard(config)) is None:
        return
    shard, total = spec
    keep = [item for i, item in enumerate(items) if i % total == shard - 1]
    drop = [item for i, item in enumerate(items) if i % total != shard - 1]
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep
