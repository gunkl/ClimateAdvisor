#!/usr/bin/env python3
"""One-off migration: rename the dev/simulated climate_advisor zones' entity_ids
to zone-prefixed names via HA's WebSocket entity-registry-update API (Issue #1000
addendum).

Background: before Issue #1000, every climate_advisor config entry registered
entities with identical friendly names, so Home Assistant disambiguated the
resulting entity_id collisions with numeric suffixes (_2, _3, _7...) assigned by
registration order at first creation. Issue #1000 fixed this going forward via
device_info + has_entity_name, but existing entity_ids are untouched by a code
deploy alone (unique_id is what HA uses to recognize "this is the same entity",
not entity_id) - a registry-level rename is a separate, explicit step.

This script targets ONLY the dev/simulated zones. The real "Zone 1" config entry
is intentionally never touched - hardcoded elsewhere (e.g. esphome-configs/
e1002-display.yaml), so its entity_ids must never change.

Renaming an entity_id cannot be done by editing core.entity_registry on disk
while HA is running - the in-memory registry is authoritative and periodically
overwrites the file, so a manual edit would be lost or desync the live registry.
The correct mechanism is the same one the Settings UI's entity-rename field
uses: the `config/entity_registry/update` WebSocket command.

Usage:
    pip install websockets   # one-time, dev-only - not a runtime dependency of
                              # the integration itself (manifest.json is untouched)
    python3 tools/rename_simulated_zone_entity_ids.py            # dry run (default)
    python3 tools/rename_simulated_zone_entity_ids.py --apply    # actually rename

Sequencing: run this AFTER deploying Issue #1000 (device_info) and Issue #1001
(sensor removal) and after HA has restarted with the new code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ha_logs import _build_ha_base_url, load_config  # noqa: E402

try:
    import websockets
except ImportError:
    print("ERROR: this script needs the 'websockets' package: pip install websockets", file=sys.stderr)
    sys.exit(1)

# The real house's config entry ("Zone 1") - NEVER rename its entities. Hardcoded
# here (not derived) so a future edit of the target-entry list below can never
# accidentally include it.
DO_NOT_TOUCH_ENTRY_ID = "01KM12CQSGFV91EPEJXSHZ5Y1K"

# The 6 dev/simulated zones' config_entry_ids, confirmed live via core.config_entries
# during Issue #1000's planning session.
TARGET_ENTRY_IDS = {
    "01M1G3GF9XDD9TMHQBBKNW2SP6": "Simulated 2",
    "01M1YCCVY6J82FFHD1RA4XDS5E": "Simulated Cold",
    "01M1YCV4FDN88SGQSM8K8YCVKY": "Simulated Hot",
    "01M2E07HP8GK45FGQTK5FC98XX": "Simulated wild",
    "01M2P0GKKK6YS0V79GTFHXJGJ1": "Simulated TOU Away",
    "01M2P0H1HYHGJ86RB7T77HDBE1": "Simulated TOU Vacation",
}

# Keys whose sensors were removed in Issue #1001 - if this script runs before
# that deploy for any reason, skip them rather than renaming an entity about to
# disappear.
REMOVED_KEYS = {"nat_vent_target_temp", "effective_target_source"}


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


async def _ws_call(ws, msg_id: int, payload: dict) -> dict:
    payload = {"id": msg_id, **payload}
    await ws.send(json.dumps(payload))
    while True:
        response = json.loads(await ws.recv())
        if response.get("id") == msg_id:
            return response


async def run(apply: bool) -> None:
    config = load_config()
    token = config.get("HA_API_TOKEN", "")
    if not token:
        print("ERROR: HA_API_TOKEN is not set in .deploy.env.", file=sys.stderr)
        sys.exit(1)

    base_url = _build_ha_base_url(config)
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

    async with websockets.connect(ws_url) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            print(f"ERROR: unexpected handshake: {hello}", file=sys.stderr)
            sys.exit(1)

        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_result = json.loads(await ws.recv())
        if auth_result.get("type") != "auth_ok":
            print(f"ERROR: authentication failed: {auth_result}", file=sys.stderr)
            sys.exit(1)

        msg_id = 1
        listing = await _ws_call(ws, msg_id, {"type": "config/entity_registry/list"})
        if not listing.get("success"):
            print(f"ERROR: could not list entity registry: {listing}", file=sys.stderr)
            sys.exit(1)

        entries = listing["result"]
        renames: list[tuple[str, str, str]] = []  # (old_entity_id, new_entity_id, zone_title)

        for entry in entries:
            entity_id = entry["entity_id"]
            unique_id = entry.get("unique_id") or ""
            config_entry_id = entry.get("config_entry_id")

            if config_entry_id == DO_NOT_TOUCH_ENTRY_ID:
                continue
            if config_entry_id not in TARGET_ENTRY_IDS:
                continue

            prefix = f"{config_entry_id}_"
            if not unique_id.startswith(prefix):
                # Not one of ClimateAdvisorBaseSensor's/ClimateAdvisorAutomationSwitch's
                # unique_id-keyed entities - skip rather than guess.
                continue
            key = unique_id[len(prefix) :]
            if key in REMOVED_KEYS:
                continue

            domain = entity_id.split(".", 1)[0]
            zone_slug = _slugify(TARGET_ENTRY_IDS[config_entry_id])
            new_entity_id = f"{domain}.{zone_slug}_{key}"

            if new_entity_id == entity_id:
                continue  # already correctly named

            renames.append((entity_id, new_entity_id, TARGET_ENTRY_IDS[config_entry_id]))

        if not renames:
            print("Nothing to rename - all target entities already have zone-prefixed entity_ids.")
            return

        print(f"{'RENAME' if apply else 'DRY RUN'} - {len(renames)} entity_id(s):\n")
        for old_id, new_id, zone_title in sorted(renames, key=lambda r: (r[2], r[0])):
            print(f"  [{zone_title}] {old_id}  ->  {new_id}")

        if not apply:
            print("\nRe-run with --apply to perform these renames.")
            return

        print()
        for old_id, new_id, zone_title in renames:
            msg_id += 1
            result = await _ws_call(
                ws,
                msg_id,
                {"type": "config/entity_registry/update", "entity_id": old_id, "new_entity_id": new_id},
            )
            if result.get("success"):
                print(f"  OK   [{zone_title}] {old_id} -> {new_id}")
            else:
                print(f"  FAIL [{zone_title}] {old_id} -> {new_id}: {result.get('error')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually perform the renames (default: dry run)")
    args = parser.parse_args()
    asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    main()
