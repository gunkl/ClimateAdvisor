"""Name a timed-out real-HA test in the run's output (Issue #1572).

``tests_ha/pytest.ini`` uses ``timeout_method = thread`` (Issue #1518): a test that runs past ``timeout`` makes
pytest-timeout dump stacks and end the whole process with ``os._exit(1)``. Under pytest-xdist that dump goes to the
worker's own terminal, which never reaches the controller, so the run only says ``worker 'gwN' crashed while running
...`` -- indistinguishable from a real crash (seen 2026-11-14: eight "crashes" on a loaded machine, all slow tests).

This plugin wraps pytest-timeout's thread-method handler: before the original dumps and exits, it suspends pytest's
output capture and writes one plain line to the worker's real stderr, which the controller does inherit::

    CA real-HA: TIMEOUT after 240 s: tests_ha/test_x.py::test_y -- ...

The original handler then runs unchanged. Nothing else is touched; a pytest-timeout without ``timeout_timer`` is
left alone.
"""

from __future__ import annotations

import os
from typing import Any

MARK = "CA real-HA: TIMEOUT"


def timeout_line(nodeid: str, seconds: float) -> str:
    return (
        f"\n{MARK} after {seconds:.0f} s: {nodeid} -- pytest-timeout (tests_ha/pytest.ini) is ending this worker;"
        " xdist will report it as 'crashed' (Issue #1572)\n"
    )


def _wrap_timeout_timer() -> None:
    try:
        import pytest_timeout  # noqa: PLC0415
    except ImportError:
        return
    original = getattr(pytest_timeout, "timeout_timer", None)
    if original is None or getattr(original, "_ca_1572", False):
        return

    def timeout_timer(item: Any, settings: Any) -> Any:
        try:
            capman = item.config.pluginmanager.getplugin("capturemanager")
            if capman is not None:
                capman.suspend_global_capture(item)
            os.write(2, timeout_line(item.nodeid, float(settings.timeout)).encode())
        except Exception:  # noqa: BLE001 - the report is best effort; the original handler must still run
            pass
        return original(item, settings)

    timeout_timer._ca_1572 = True  # type: ignore[attr-defined]
    pytest_timeout.timeout_timer = timeout_timer


def pytest_configure(config: Any) -> None:
    _wrap_timeout_timer()
