#!/usr/bin/env bash
# Issue #1745: say out loud when a CI job ran out of time, and warn before it does.
#
#   ci_job_times.sh timeouts   a job this run that was cancelled gets an ::error:: annotation naming how long it ran
#                              and its limit ("timed out or was cancelled after 8 min (limit 8 min)"). A job that hits
#                              its timeout-minutes is reported by Actions as "cancelled", and the gate jobs only echo
#                              `ha-slice=cancelled`, which does not say why (Issues #1743, #1745).
#   ci_job_times.sh warn       a job that finished having used more than 75% of its limit gets a ::warning::, so a
#                              creeping job is seen while it still passes. Never fails.
#
# Environment: GH_TOKEN (actions: read), GH_REPO, RUN_ID, and LIMITS - one "<job name>=<minutes>" per line, the
# job's timeout-minutes. The name is the job's display name with each "(K/N)" / "(shard K/N)" index replaced by "*"
# (`ha-real (*/4)`, `generated-days (shard */4)`); a job not listed has no known limit and is ignored.
# tests/test_ci_job_times_1745.py keeps LIMITS equal to the workflows' timeout-minutes.
#
# This script only reports: it always exits 0, so it can never change a gate's verdict.
set -uo pipefail

mode="${1:-}"
case "$mode" in
  timeouts | warn) ;;
  *)
    echo "usage: ci_job_times.sh timeouts|warn" >&2
    exit 2
    ;;
esac

# "<name> TAB <conclusion> TAB <seconds the job ran>" for the jobs of this run that have finished. A missing or
# unreadable timestamp gives 0 seconds rather than an error (the report must not break on one odd row).
if ! jobs=$(gh api "repos/$GH_REPO/actions/runs/$RUN_ID/jobs" --paginate \
  --jq '.jobs[] | select(.conclusion != null) | [.name, .conclusion, (try (((.completed_at // .started_at) | fromdateiso8601) - (.started_at | fromdateiso8601)) catch 0)] | @tsv'); then
  echo "::notice title=Job times::could not read this run's jobs from the API; no timing report"
  exit 0
fi

while IFS=$'\t' read -r name conclusion secs; do
  [ -z "$name" ] && continue
  key=$(printf '%s' "$name" | sed -E 's/\((shard )?[0-9]+\/([0-9]+)\)/(\1*\/\2)/')
  limit=$(printf '%s\n' "$LIMITS" | awk -v k="$key" '{ sub(/^[ \t]+/, ""); sub(/[ \t]+$/, "") } index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }')
  case "$limit" in '' | *[!0-9]*) continue ;; esac
  secs=${secs%.*}
  mins=$(((secs + 30) / 60))
  if [ "$mode" = timeouts ]; then
    if [ "$conclusion" = cancelled ]; then
      echo "::error title=Job timed out::${name} timed out or was cancelled after ${mins} min (limit ${limit} min)"
    fi
  elif [ "$conclusion" != cancelled ] && [ $((secs * 4)) -gt $((limit * 60 * 3)) ]; then
    echo "::warning title=Job near its time limit::${name} ran ${mins} min of its ${limit} min limit ($((secs * 100 / (limit * 60)))%); find the slow step before it times out"
  fi
done <<< "$jobs"
exit 0
