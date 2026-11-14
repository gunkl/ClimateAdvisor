"""English rendering of a ``Msg`` for stored records and the AI input (Issue #1341, D4).

Records (the event log ``reason``/``detail``, ``last_action_reason``, the activity record text fed to the AI
investigator) keep English bytes; display languages re-render the sibling ``*_key``/``*_params``. Production loads
the catalogs once in ``async_setup_entry``. Headless callers (the simulation harness, unit tests, tools) never run
setup, so the first record render loads them: ``load_catalogs`` is idempotent and a one-time read of small JSON
files, so a record is never the bare catalog key.
"""

from __future__ import annotations

from typing import Any

from . import i18n


def record_en(value: Any) -> Any:
    """English text of a ``Msg``; anything else (a legacy plain ``str``, ``None``) passes through unchanged."""
    if isinstance(value, i18n.Msg):
        if not i18n.catalogs_loaded():
            i18n.load_catalogs()
        return i18n.render_en(value)
    return value
