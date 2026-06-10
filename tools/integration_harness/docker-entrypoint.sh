#!/bin/sh
# Entrypoint wrapper for the Climate Advisor integration harness.
#
# When FAKETIME is non-empty, injects libfaketime via LD_PRELOAD so that
# all time-related syscalls inside HA (and the climate_advisor integration)
# see the faked time.  When FAKETIME is empty, passes through to the normal
# HA entrypoint with zero overhead.
#
# The normal HA container entrypoint is /init (s6-overlay).

# Alpine Linux path (the HA base image is Alpine-based)
FAKETIME_SO="/usr/lib/faketime/libfaketime.so.1"

if [ -n "${FAKETIME}" ]; then
    echo "[integration-harness] Activating libfaketime: FAKETIME=${FAKETIME}"
    export LD_PRELOAD="${FAKETIME_SO}"
else
    echo "[integration-harness] FAKETIME not set — running at real wall-clock time"
fi

# Exec the standard s6-overlay init (the normal HA entrypoint)
exec /init "$@"
