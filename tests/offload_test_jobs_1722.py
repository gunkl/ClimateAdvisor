"""Test-only job functions run by the worker child (Issue #1722). Imported by name from tests/."""

from __future__ import annotations

import importlib
import logging
import os
import sys
import time
from typing import Any

_LOG = logging.getLogger("offload_test_jobs")


def echo(payload: dict[str, Any]) -> Any:
    """Return a structure with a tuple and nested data (tuple becomes a list over JSON)."""
    return {"sum": sum(payload["xs"]), "t": (1, 2), "payload": payload}


def boom(payload: dict[str, Any]) -> Any:
    """Log at two levels then raise."""
    _LOG.warning("about to fail x=%s", payload.get("x"))
    _LOG.info("info line")
    raise RuntimeError("kaboom")


def sleeper(payload: dict[str, Any]) -> Any:
    """Sleep a long time (killed by the parent)."""
    time.sleep(payload.get("s", 60))
    return None


def probe_isolation(payload: dict[str, Any]) -> Any:
    """Report whether a planted module is importable and what sys.path holds."""
    try:
        importlib.import_module(payload["name"])
        found = True
    except ImportError:
        found = False
    return {"found": found, "pid": os.getpid(), "cwd_on_path": "" in sys.path}


def ha_stub_report(payload: dict[str, Any]) -> Any:
    """Describe the homeassistant modules and the dt stub."""
    dt = sys.modules["homeassistant.util.dt"]
    return {
        "ha": sorted(m for m in sys.modules if m.split(".")[0] == "homeassistant"),
        "dt": sorted(a for a in vars(dt) if not a.startswith("_")),
        "attr": sys.modules["homeassistant.util"].dt is dt,
    }


def import_closure(payload: dict[str, Any]) -> Any:
    """Import the given package modules and report homeassistant modules in sys.modules."""
    for name in payload["modules"]:
        importlib.import_module(f"custom_components.climate_advisor.{name}")
    return sorted(m for m in sys.modules if m.split(".")[0] == "homeassistant")


def noisy(payload: dict[str, Any]) -> Any:
    """Print to stdout and stderr (non-JSON), return ok."""
    print("stray stdout")
    sys.stderr.write("raw stderr line\n")
    return "ok"


def not_json(payload: dict[str, Any]) -> Any:
    """Return something that is not JSON-able."""
    return {1, 2}


def hard_exit(payload: dict[str, Any]) -> Any:
    """Die without writing an envelope."""
    os._exit(3)
