# Dockerfile for Climate Advisor integration harness.
#
# Extends the official HA stable image with:
#   - libfaketime  — time control via LD_PRELOAD + FAKETIME env var
#   - anthropic    — required by climate_advisor manifest.json
#   - curl         — used by healthcheck and by the entrypoint token script
#
# TIME CONTROL
# ============
# Set the FAKETIME env var when running the container:
#   FAKETIME="@2026-01-15 08:00:00"   — freeze to a specific moment
#   FAKETIME="+3600"                   — offset real time by 1 hour
#   FAKETIME="i1.5"                    — accelerate time 1.5x
# When FAKETIME is set the entrypoint injects:
#   LD_PRELOAD=/usr/lib/faketime/libfaketime.so.1   (Alpine path)
# into the HA process environment.  The so.1 path is standard on Debian/Ubuntu.
#
# NOTES
# =====
# - libfaketime intercepts gettimeofday/clock_gettime at the libc level.
#   Python's time.time() and datetime.now() both go through glibc, so
#   dt_util.utcnow() / dt_util.now() inside the integration see the faked time.
# - anthropic is pre-installed to avoid a network call on every container start.
#   HA's integration loader would pip-install it anyway, but this is faster for CI.

FROM ghcr.io/home-assistant/home-assistant:2025.5.3

# Install libfaketime and curl (healthcheck uses curl).
# Base image is Alpine Linux — use apk, not apt-get.
# libfaketime.so.1 lands at /usr/lib/faketime/libfaketime.so.1 on Alpine.
RUN apk add --no-cache libfaketime curl

# Pre-install the anthropic package (climate_advisor manifest.json requirement).
# HA will re-verify on startup but won't re-download if already installed.
RUN pip3 install --no-cache-dir "anthropic>=0.49.0"

# Wrapper entrypoint: injects LD_PRELOAD when FAKETIME is set, then execs the
# normal HA entrypoint.  The conditional ensures zero overhead when time control
# is not needed (FAKETIME empty or unset).
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
