"""Shared entry-scoped remote storage path resolution for the SSH diagnostic tools.

Since Issue #796, Climate Advisor's storage files are entry-scoped
(``climate_advisor_learning_<entry_id>.json`` etc. — see
``custom_components/climate_advisor/storage_paths.py::resolve_entry_scoped_path``).
The diagnostic tools (``learning_db.py``, ``engine_status.py``,
``thermal_replay.py``) hardcoded the pre-#796 unscoped path and silently broke
for any multi-zone install. This module is the single place those tools
resolve a remote filename, reusing the real component's own scoping function
rather than a second hand-rolled copy of it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "climate_advisor"))
from storage_paths import resolve_entry_scoped_path  # noqa: E402

REMOTE_CONFIG_DIR = PurePosixPath("/config")

LEARNING_DB_FILE = "climate_advisor_learning.json"
STATE_FILE = "climate_advisor_state.json"
CHART_LOG_FILE = "climate_advisor_chart_log.json"
CYCLE_FILE = "climate_advisor_cycles.json"  # Issues #1512/#1516: the pooled cycle model's per-zone cycle file


def remote_path(base_filename: str, entry_id: str | None) -> str:
    """Return the remote path for base_filename, scoped to entry_id if given."""
    return str(resolve_entry_scoped_path(REMOTE_CONFIG_DIR, base_filename, entry_id or ""))


def entry_id_from_remote_path(base_filename: str, path: str) -> str | None:
    """The entry_id a ``remote_path(base_filename, entry_id)`` result was scoped to (None for the unscoped path)."""
    if path == remote_path(base_filename, None):
        return None
    stem = PurePosixPath(base_filename).stem + "_"
    name = PurePosixPath(path).stem
    return name[len(stem) :] if name.startswith(stem) and len(name) > len(stem) else None


def discover_entry_ids(config: dict, base_filename: str, build_cmd) -> list[str]:
    """List entry_ids with an existing entry-scoped file for base_filename on the remote host.

    ``build_cmd(config, remote_command_str) -> list[str]`` builds the full
    subprocess argv for running remote_command_str over SSH — callers differ
    in how they assemble ssh args (some bundle the host target into a single
    helper, some keep it separate), so this takes a single adapter instead of
    assuming either shape.

    E.g. for 'climate_advisor_learning.json', finds
    'climate_advisor_learning_<entry_id>.json' files and returns the
    extracted entry_ids. Returns [] on any SSH/parse failure (caller falls
    back to the legacy unscoped path, matching pre-#796 single-zone behavior).
    """
    stem, ext = base_filename.rsplit(".", 1)
    pattern = str(REMOTE_CONFIG_DIR / f"{stem}_*.{ext}")
    cmd = build_cmd(config, f"ls {pattern} 2>/dev/null")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return []
    entry_ids = []
    prefix = f"{stem}_"
    for line in result.stdout.splitlines():
        name = line.strip().rsplit("/", 1)[-1]
        if name.startswith(prefix) and name.endswith(f".{ext}"):
            entry_ids.append(name[len(prefix) : -(len(ext) + 1)])
    return entry_ids


def resolve_remote_path_with_discovery(
    config: dict,
    base_filename: str,
    entry_id: str | None,
    build_cmd,
) -> str:
    """Resolve the remote path to read, auto-discovering the zone when possible.

    ``build_cmd(config, remote_command_str) -> list[str]`` — see discover_entry_ids.

    - entry_id given: use the scoped path directly (caller's explicit choice).
    - entry_id not given: look for entry-scoped files on the remote host.
        - Exactly one found: use it, printing which zone was auto-selected.
        - Multiple found: exit with an error listing them, asking for --entry-id.
        - None found: fall back to the legacy unscoped path (single-zone
          install, or a zone that hasn't migrated yet).
    """
    if entry_id:
        return remote_path(base_filename, entry_id)

    found = discover_entry_ids(config, base_filename, build_cmd)
    if len(found) == 1:
        print(f"(auto-selected zone entry_id={found[0]} — pass --entry-id to target a specific zone)", file=sys.stderr)
        return remote_path(base_filename, found[0])
    if len(found) > 1:
        print(
            f"ERROR: multiple zones have their own {base_filename}: {', '.join(found)}. Pass --entry-id to pick one.",
            file=sys.stderr,
        )
        sys.exit(1)
    # No entry-scoped file found — legacy unscoped path (pre-#796 single-zone).
    return remote_path(base_filename, None)


# ---------------------------------------------------------------------------
# REST zone scoping (Issue #1418)
# ---------------------------------------------------------------------------
#
# Every Climate Advisor REST view answers for ONE zone. With several zones and
# no ``?zone=``, a GET answers for the first zone (and raises the "Ambiguous
# zone selection" Repairs card) — so a tool that sends no zone silently reports
# Zone 1 while the user thinks it is looking at another zone. Tools resolve the
# zone here: an explicit --entry-id is sent as ``?zone=``; without one, a
# multi-zone install is detected from GET /status and the tool must ask.

STATUS_API_PATH = "/api/climate_advisor/status"


class ZoneSelectionRequired(Exception):  # noqa: N818 - a signal, not an error state of the tool itself
    """Several zones are loaded and no --entry-id was given; ``zones`` lists them."""

    def __init__(self, zones: list[dict]) -> None:
        super().__init__(f"{len(zones)} Climate Advisor zones are loaded; pass --entry-id")
        self.zones = zones


def with_zone(url: str, entry_id: str | None) -> str:
    """``url`` with ``zone=<entry_id>`` appended (unchanged when ``entry_id`` is None/empty)."""
    if not entry_id:
        return url
    from urllib.parse import quote

    return f"{url}{'&' if '?' in url else '?'}zone={quote(entry_id, safe='')}"


def format_zone_choices(zones: list[dict]) -> str:
    """One ``  <entry_id>  <title>`` line per zone, for an error message."""
    return "\n".join(f"  {z.get('entry_id', '?')}  {z.get('title') or ''}".rstrip() for z in zones)


def _get_json(url: str, ha_token: str, timeout: float = 10) -> dict | None:
    import json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {ha_token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError, OSError, ValueError:
        return None
    return data if isinstance(data, dict) else None


def resolve_api_zone(ha_url: str, ha_token: str, entry_id: str | None, *, get_json=_get_json) -> str | None:
    """The zone to send on REST calls: ``entry_id`` when given, else None for a single-zone install.

    Raises ZoneSelectionRequired when no ``entry_id`` was given and /status reports several zones. When /status
    can't be read, returns None (the caller's own request reports the failure).
    """
    if entry_id:
        return entry_id
    status = get_json(f"{ha_url.rstrip('/')}{STATUS_API_PATH}", ha_token)
    if status and status.get("zone_selection_required"):
        raise ZoneSelectionRequired(list(status.get("zones") or []))
    return None
