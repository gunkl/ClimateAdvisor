"""Registers the built-in AI providers into ``ai_backend.PROVIDERS``.

``ai_backend`` imports no provider, so whoever needs ``get_provider`` (config flow, setup,
coordinator) first calls this. Importing the provider modules registers their specs; importing
``claude_api`` also imports the Anthropic SDK (guarded), so the async variant runs it in the
executor. Idempotent. A provider that fails to import is logged and left out; the others
still register.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

_PROVIDER_MODULES = ("claude_api",)


def ensure_providers_registered() -> None:
    """Import the provider modules so their specs are in ``PROVIDERS`` (sync, idempotent, never raises)."""
    for name in _PROVIDER_MODULES:
        try:
            importlib.import_module(f"{__package__}.{name}")
        except Exception:  # noqa: BLE001 - one provider failing must not disable the others
            _LOGGER.exception("AI provider module %s could not be loaded; that provider is unavailable", name)


async def async_ensure_providers_registered(hass: Any) -> None:
    """Same, with the first-time module imports kept off the event loop."""
    await hass.async_add_executor_job(ensure_providers_registered)
