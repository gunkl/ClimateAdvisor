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


class MsgText(str):
    """The English text of a ``Msg`` that still carries the message (``.msg``).

    A reason/detail written as ``MsgText`` behaves as the exact English ``str`` everywhere (comparison, ``in``,
    ``startswith``, JSON, f-strings, test assertions) while the event log can also store ``<field>_key`` /
    ``<field>_params`` for display in another language (``coordinator._emit_event`` reads ``.msg``). Joining it
    with another string (``+``, an f-string) yields a plain ``str``; a composite sentence must be one ``Msg``.
    """

    msg: i18n.Msg

    def __new__(cls, msg: i18n.Msg) -> MsgText:
        obj = super().__new__(cls, record_en(msg))
        obj.msg = msg
        return obj


def msg_text(value: Any) -> Any:
    """``Msg`` -> ``MsgText`` (English text plus the message); ``str``/``MsgText``/``None`` pass through unchanged."""
    return MsgText(value) if isinstance(value, i18n.Msg) else value


def msg_of(value: Any) -> i18n.Msg | None:
    """The ``Msg`` a value carries (a ``Msg`` itself or a ``MsgText``), else ``None``."""
    if isinstance(value, i18n.Msg):
        return value
    return getattr(value, "msg", None) if isinstance(value, MsgText) else None
