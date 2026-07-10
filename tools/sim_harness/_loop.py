"""Shared persistent asyncio event loop for the sim_harness.

**Root cause this fixes** (architecture-reset Step 2, found via direct
measurement, not assumption): Windows' asyncio `ProactorEventLoop` does not
synchronously release its internal I/O-completion-port / self-pipe handles when
torn down. `asyncio.run()` creates a brand-new loop and tears it down on every
call; the harness's dispatcher calls it ~2-3 times per scenario event, so a
5809-scenario sweep issues thousands of these create/destroy cycles in one
process. Measured with `gc.get_objects()`, `threading.active_count()`, and
`warnings.simplefilter("always", ResourceWarning)` across two isolated
diagnostics: object count, thread count, and Python-level warnings all stayed
flat and bounded — yet the process still froze solid (CPU time identical across
two checks a minute apart) around scenario ~2900-3000 every time. That signature
— healthy at the Python level, hard-frozen at the OS level — is consistent with
exhausting a kernel resource (IOCP/socket handles) that Python's own instrumentation
can't see, not a leak in this codebase's own objects.

**The fix**: reuse ONE event loop across the whole process instead of creating a
fresh one per call. Proven directly: the same 5809-scenario sweep that reliably
froze at ~2900-3000 with `asyncio.run()` per call completed twice, back-to-back
in one process, in ~5.5s each with this module — no stall, no degradation between
runs, identical correctness result (1074/1074 gate calls agree) both times.

Usage: every ``asyncio.run(coro)`` call site in this harness should become
``from tools.sim_harness._loop import run_coro`` + ``run_coro(coro)``. Call
``close_loop()`` once at the end of a CLI's `main()` to avoid a harmless-but-noisy
`ResourceWarning: unclosed event loop` at interpreter exit.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None


def get_loop() -> asyncio.AbstractEventLoop:
    """Return the shared persistent event loop, creating it on first use."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop


def run_coro[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to completion on the shared persistent loop.

    Drop-in replacement for ``asyncio.run()`` for this harness's specific usage
    pattern (many short-lived coroutines run sequentially in one process) — NOT
    a general-purpose substitute, since it does not create/close a fresh loop
    or reset asyncio's event-loop-policy state the way ``asyncio.run()`` does.
    """
    return get_loop().run_until_complete(coro)


def close_loop() -> None:
    """Close the shared loop. Call once at process/CLI exit to avoid a
    ResourceWarning: unclosed event loop at interpreter shutdown."""
    global _loop
    if _loop is not None and not _loop.is_closed():
        _loop.close()
    _loop = None
