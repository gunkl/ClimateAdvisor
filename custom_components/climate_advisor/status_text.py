"""Occupant-facing status text that carries its stable code and its translatable message (Issue #1341, L4).

A ``CodedText`` IS the existing English string (``str`` subclass, ``render_en`` of its message), so every reader that
compares or logs the text keeps working; ``code`` is the stable identifier code compares, and ``msg`` is the
``i18n.Msg`` a surface renders in the viewer's language. Text built from plain prose (no catalog message yet) has
``msg=None`` and renders as itself.
"""

from __future__ import annotations

from typing import Any

from . import i18n


class CodedText(str):
    """The English text of a status line plus ``code`` (stable) and ``msg`` (translatable, may be None)."""

    code: str
    msg: i18n.Msg | None
    fan_detail_included: bool = False  # the automation status line already names the occupant's fan requests

    def __new__(cls, code: str, msg: i18n.Msg | None = None, text: str | None = None) -> CodedText:
        value = text if text is not None else (i18n.render_en(msg) if msg is not None else "")
        obj = super().__new__(cls, value)
        obj.code = code
        obj.msg = msg
        return obj

    def __reduce__(self) -> tuple[Any, ...]:  # pickle / deepcopy: rebuild from the three parts
        return (CodedText, (self.code, self.msg, str(self)))


code_of = i18n.code_of  # the one shared readers (Msg, MsgText, CodedText, CardText); re-exported here
msg_of = i18n.msg_of


def wire_of(value: Any) -> dict | None:
    """``Msg.to_wire()`` of a status value's message, or None."""
    msg = msg_of(value)
    return msg.to_wire() if msg is not None else None


def render_text(value: Any, lang: str) -> str:
    """The text in ``lang`` (plain text and non-Msg values pass through)."""
    msg = msg_of(value)
    return i18n.render(msg, lang) if msg is not None else (str(value) if value is not None else "")


def with_siblings(payload: dict, field: str, value: Any, lang: str, *, code: bool = False) -> None:
    """Set ``payload[field]`` to the text in ``lang`` and add ``<field>_msg`` (and ``<field>_code``) siblings."""
    payload[field] = render_text(value, lang) if isinstance(value, str) else value
    payload[f"{field}_msg"] = wire_of(value)
    if code:
        payload[f"{field}_code"] = code_of(value)


# The fan-status tokens (the documented "Fan Status Values" enum: what the sensors, the dashboard and the guards
# compare) -> stable code. The English token IS the text of its catalog message, so a token stays comparable.
FAN_STATUS_CODES: dict[str, tuple[str, str]] = {
    "disabled": ("disabled", "status.fan_status.disabled"),
    "inactive": ("inactive", "status.fan_status.inactive"),
    "active": ("active", "status.fan_status.active"),
    "active (unconfirmed)": ("active_unconfirmed", "status.fan_status.active_unconfirmed"),
    "running (manual override)": ("running_manual_override", "status.fan_status.running_manual_override"),
    "off (manual override)": ("off_manual_override", "status.fan_status.off_manual_override"),
    "running (untracked)": ("running_untracked", "status.fan_status.running_untracked"),
    "running (thermostat)": ("running_thermostat", "status.fan_status.running_thermostat"),
    "nat-vent (session active, fan idle)": ("nat_vent_idle", "status.fan_status.nat_vent_idle"),
}


def fan_status_text(token: str) -> CodedText:
    """A fan-status token as a ``CodedText`` (a token outside the enum keeps its text with code ``unknown``)."""
    if isinstance(token, CodedText):
        return token
    entry = FAN_STATUS_CODES.get(token)
    if entry is None:
        return CodedText("unknown", None, text=str(token))
    return CodedText(entry[0], i18n.Msg(entry[1]), text=str(token))
