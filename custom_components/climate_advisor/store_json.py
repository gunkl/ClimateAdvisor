"""One shared JSON writer/reader for the large on-disk stores and the worker pipe (Issue #1730).

Why: ``json.loads`` / ``json.dumps`` of a 3-9 MB document is one C call that holds the GIL for 84-230 ms on x86 (several
times that on a Raspberry Pi), and an executor thread does not give the GIL back in the middle of it, so the event loop
stalls for the whole call. The same document written one element per line is encoded and decoded one element at a time:
each call holds the GIL for about 0.02 ms, and the loop thread gets it back between elements.

The file is still plain JSON. ``json.loads`` (and so every older release's reader) reads it unchanged; the only
difference is where the newlines are::

    {
    "v":1,
    "leads":["1h","4h"],
    "sensors":{
    "sensor.hall":{
    "1768000000":[68.1,68.4],
    "1768001800":[68.2,null]
    }
    }
    }

``dumps(obj, depth=N)`` puts one member per line in every dict or list above nesting level ``N`` that is large or holds
containers; everything below that level is written inline by one call. ``loads`` reads such a layout line by line, and
anything else (a one-line file written by an older release, a pretty-printed one) whole.

NaN and infinity: orjson writes them as ``null`` (and non-ASCII as raw UTF-8); the stdlib writes ``NaN`` / ``Infinity``
tokens (and ``\\uXXXX`` escapes). A store whose readers do not all accept ``null`` where a NaN could be stored passes
``lossless=True`` and keeps the stdlib's output, still one element per line. ``loads`` accepts both: orjson rejects the
``NaN`` / ``Infinity`` tokens older releases wrote, and a file holding them is then read whole by ``json.loads``.

Hass-free and imports nothing from the package, so the worker process can use it.
"""

from __future__ import annotations

import json
from typing import Any

import orjson

_OPTIONS = orjson.OPT_NON_STR_KEYS  # stdlib turns int/float/bool/None dict keys into strings; so do we
_LARGE = 1024  # a container of plain values (no nested container) is split above this many members
_MISSING = object()
_CLOSERS: dict[bytes, type[dict[Any, Any]] | type[list[Any]]] = {b"}": dict, b"]": list}


_STDLIB = json.JSONEncoder(separators=(",", ":"))  # json.dumps(value, separators=...) builds one per call


def _encode(value: Any, lossless: bool) -> bytes:
    if lossless:
        return _STDLIB.encode(value).encode()
    return orjson.dumps(value, option=_OPTIONS)


def _key_prefix(key: Any, lossless: bool) -> bytes:
    """``"key":`` as JSON, the key converted as the stdlib does."""
    return _encode({key: None}, lossless)[1:-5]


def _splits(value: Any, level: int, depth: int) -> bool:
    if level >= depth or not isinstance(value, dict | list) or not value:
        return False
    members = value.values() if isinstance(value, dict) else value
    return len(value) > _LARGE or any(isinstance(m, dict | list) for m in members)


def _emit(value: Any, level: int, depth: int, prefix: bytes, lossless: bool, out: list[bytes]) -> None:
    if not _splits(value, level, depth):
        out.append(prefix + _encode(value, lossless))
        return
    is_dict = isinstance(value, dict)
    out.append(prefix + (b"{" if is_dict else b"["))
    last = len(value) - 1
    for i, (key, member) in enumerate(value.items() if is_dict else enumerate(value)):
        if _splits(member, level + 1, depth):
            _emit(member, level + 1, depth, _key_prefix(key, lossless) if is_dict else b"", lossless, out)
            if i < last:
                out[-1] += b","  # its closer
            continue
        # a member on one line: one encode call, the key included (``{"k":v}`` less its braces is ``"k":v``)
        line = _encode({key: member}, lossless)[1:-1] if is_dict else _encode(member, lossless)
        out.append(line + b"," if i < last else line)
    out.append(b"}" if is_dict else b"]")


def dumps(obj: Any, *, depth: int = 2, lossless: bool = False) -> bytes:
    """``obj`` as JSON bytes, one member per line in the containers above nesting level ``depth`` (see module doc).

    Raises ``TypeError`` (orjson's ``JSONEncodeError`` is one) or ``ValueError`` for what the stdlib refuses too.
    """
    out: list[bytes] = []
    _emit(obj, 0, depth, b"", lossless, out)
    return b"\n".join(out)


def _loads_lines(data: bytes) -> Any:
    root: Any = _MISSING
    stack: list[Any] = []
    for raw in data.split(b"\n"):
        line = raw.strip()
        if line[-1:] == b",":
            line = line[:-1]
        if not line:
            continue
        parent = stack[-1] if stack else None
        if line in _CLOSERS:
            if not isinstance(parent, _CLOSERS[line]):
                raise ValueError("unbalanced closer")
            stack.pop()
            continue
        if parent is None and (root is not _MISSING or line[-1:] not in (b"{", b"[")):
            raise ValueError("not a line-per-element layout")
        opener = line[-1:]
        if opener in (b"{", b"["):
            member: Any = {} if opener == b"{" else []
            head = line[:-1]
        else:
            member, head = _MISSING, line
        if parent is None:
            root = member
        elif isinstance(parent, dict):
            if member is _MISSING:
                parent.update(orjson.loads(b"{" + head + b"}"))
            else:
                parent[next(iter(orjson.loads(b"{" + head + b"null}")))] = member
        else:
            if member is not _MISSING and head:
                raise ValueError("a list member has no key")
            parent.append(orjson.loads(line) if member is _MISSING else member)
        if member is not _MISSING:
            stack.append(member)
    if stack or root is _MISSING:
        raise ValueError("truncated document")
    return root


def loads(data: bytes | str) -> Any:
    """Parse ``dumps`` output or any other JSON document (see module doc). Raises ``ValueError`` like ``json.loads``."""
    if isinstance(data, str):
        data = data.encode()
    # one element per line (at least two newlines); a one-line file has nothing to split. ``find`` (memchr) stops at
    # the second newline, where ``count`` scans the whole file in one call (5-10 ms for 4.5 MB on x86)
    first = data.find(b"\n")
    if first != -1 and data.find(b"\n", first + 1) != -1:
        try:
            return _loads_lines(data)
        except Exception:  # any other layout (or NaN/Infinity): read whole, below
            pass
    try:
        return orjson.loads(data)
    except orjson.JSONDecodeError:
        return json.loads(data)  # NaN / Infinity / out-of-range numbers from older releases
