# Climate Advisor — Tier B Integration Harness

This directory contains the Tier B integration harness for Climate Advisor.
It boots a real Home Assistant instance in Docker and drives it via the REST/WebSocket API
to exercise behavior that the headless Tier A harness (`tools/sim_harness/`) cannot reach:
the coordinator state-listener layer (`_async_thermostat_changed`), HA restart,
periodic update cycles, and real timer firing.

## Architecture

```
tools/integration_harness/
├── Dockerfile              # Extends HA:2025.5.3 with libfaketime + anthropic
├── docker-compose.yml      # Compose stack: one service (homeassistant)
├── docker-entrypoint.sh    # Injects LD_PRELOAD when FAKETIME is set
├── conftest.py             # pytest fixtures: boot stack, auth, teardown
├── test_integration_boot.py # Foundation milestone: HA up + integration loaded
├── config/                 # Live HA config (bind-mounted; mutated at runtime)
│   ├── configuration.yaml  # Minimal HA config: demo, http, logger, frontend
│   └── .storage/           # Pre-seeded HA storage (reset from config-seed/ at test start)
└── config-seed/            # Canonical seed state (git-tracked, read-only)
    ├── configuration.yaml
    └── .storage/
        ├── auth                          # testadmin user record + credential
        ├── auth_provider.homeassistant   # bcrypt password hash for testadmin
        ├── core.config_entries           # Pre-seeded climate_advisor config entry
        └── onboarding                    # Marks onboarding as done
```

## Prerequisites

- Docker Desktop (Windows) or Docker Engine (Linux/CI) with Compose v2
- Python packages: `pytest`, `requests` (`pip install pytest requests`)
- On **Windows**: Docker must be running via WSL 2 engine (Docker Desktop default)
  The conftest auto-detects Windows and prefixes Docker commands with `wsl`

## Running the tests

```bash
# From the repo root:
pytest tools/integration_harness/ -m integration -v

# With time control (freeze HA to a specific moment):
FAKETIME="@2026-01-15 08:00:00" pytest tools/integration_harness/ -m integration -v
```

The `integration` mark gates the tests so they **never run** as part of the normal
`pytest tests/` unit suite. Docker availability is also checked at startup — if Docker
is unreachable the entire integration session is skipped (not failed).

## Manual stack management

```bash
# Start stack (builds image if needed)
cd tools/integration_harness
docker compose up --build -d

# Watch logs
docker compose logs -f

# Check HA API
curl http://localhost:18123/api/   # → {"message": "API running."} (after auth)

# Stop and remove all containers + networks
docker compose down -v
```

HA is exposed on **port 18123** to avoid colliding with a real HA on 8123.

## Auth setup

The pre-seeded user is `testadmin` / `testpassword123`. This is stored as a
bcrypt hash in `config-seed/.storage/auth_provider.homeassistant`.

HA uses a 3-step auth flow, not a simple password grant:

1. `POST /auth/login_flow` — initiate flow, get `flow_id`
2. `POST /auth/login_flow/{flow_id}` — submit credentials, get one-time `code`
3. `POST /auth/token` — exchange `code` for a short-lived JWT Bearer token (~30 min)

The `ha_token` and `ha_headers` fixtures in `conftest.py` handle this automatically.

### Why not a long-lived access token (LLAT)?

LLATs are JWT-signed with a random `jwt_key` stored in `.storage/auth`.
Pre-seeding a valid LLAT requires generating a matching JWT offline, which is fragile.
The 3-step flow generates a fresh token each test session from the known credentials,
which is simpler and more robust.

## Time control (libfaketime)

The Docker image includes `libfaketime` (Alpine package `libfaketime-0.9.10`).
The library is at `/usr/lib/faketime/libfaketime.so.1`.

To freeze HA at a specific time, set `FAKETIME` before starting the stack:

```bash
# Freeze time at 2026-01-15 08:00 local
FAKETIME="@2026-01-15 08:00:00" docker compose up --build -d

# Advance time 1 hour ahead of wall clock
FAKETIME="+3600" docker compose up --build -d

# Run at 2x speed
FAKETIME="i1.0" docker compose up --build -d
```

When `FAKETIME` is set, `docker-entrypoint.sh` injects:
```
LD_PRELOAD=/usr/lib/faketime/libfaketime.so.1
```
into the HA process. This intercepts `gettimeofday` and `clock_gettime` at the
libc level, so `dt_util.utcnow()`, `dt_util.now()`, and `time.time()` inside the
integration all see the faked time.

When `FAKETIME` is **not set**, HA runs at real wall-clock time with zero overhead.

## Seed state idempotency

HA mutates `.storage/` files during every run (adding system tokens, entity
registries, etc.). The `ha_stack` fixture calls `_reset_config_to_seed()` at
the start of every test session, which copies `config-seed/` → `config/` and
deletes any `.storage/` files that are not in the seed. This ensures
test sessions are deterministic regardless of previous run state.

## CI notes

This harness is intended for **on-demand** and **pre-release** validation, not
per-commit CI. Recommended gate: pull-request merge (not every push).

```yaml
# Example GitHub Actions step:
- name: Integration tests
  run: pytest tools/integration_harness/ -m integration -v --timeout=180
  env:
    DOCKER_BUILDKIT: "1"
```

## Foundation milestone (completed)

The `test_integration_boot.py` file proves the foundation:
- HA 2025.5.3 boots in Docker in < 90s
- `climate_advisor` config entry state = `loaded`
- 18 `sensor.climate_advisor_*` entities + 1 `switch.climate_advisor_*` are visible
- Demo climate (`climate.ecobee`) and weather (`weather.demo_weather_north`) entities present

## Next step: scenario replay (Tier B)

The following `track: integration` scenarios need scenario replay infrastructure
(a separate future task from the foundation):

| Scenario | Key assertion | Why integration-only |
|---|---|---|
| `away_setpoint_change_not_override.json` | `override_not_detected` | CA's own setback must not trigger `_async_thermostat_changed` override detection |
| Any HA restart scenario | State survival | Requires actual coordinator restart lifecycle |
| Timer-fired grace expiry | Timer fires on real clock | `async_call_later` timing is not exercisable headless |

Scenario replay design will:
1. Use a time-control fixture to freeze/advance FAKETIME
2. Drive HA state via `POST /api/services/climate/set_temperature` etc.
3. Poll `GET /api/states/sensor.climate_advisor_*` and assert expected values
4. Map scenario JSON `assertions` → REST API checks
