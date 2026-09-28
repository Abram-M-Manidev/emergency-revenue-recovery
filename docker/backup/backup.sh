#!/bin/sh
# ERRS backup scheduler: runs `errs-offsite run-once` on an interval.
#
# The work — pg_dump, archive validation, the B2 upload, read-back
# verification, local retention — lives in errs_offsite.py, where it is
# locked, tested and type-checked. This loop only decides *when*.
#
# A shell loop rather than cron: one nightly job needs no scheduler, and a
# cron daemon in a container adds a second thing that can be running while
# appearing not to be. `restart: unless-stopped` brings the loop back after
# a crash, a reboot or a deploy, and the first cycle runs immediately on
# start. The trade-off is that the schedule is relative to container start
# rather than wall-clock — restart the service at a quiet hour if the exact
# time matters (docs/RUNBOOK.md).
#
# Timing:
#   - after a successful cycle: sleep BACKUP_INTERVAL_SECONDS (default 1 day)
#   - after a failed cycle:     sleep BACKUP_RETRY_SECONDS (default 1 hour),
#     then run again with --if-due, which re-attempts pending uploads but
#     does not take another dump while the last one is still recent — a B2
#     outage must not turn into a new dump every hour.

set -eu

INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"
RETRY="${BACKUP_RETRY_SECONDS:-3600}"

log() {
	echo "{\"event\":\"$1\",\"service\":\"backup\",\"detail\":\"${2:-}\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
}

# As PID 1, sh ignores SIGTERM unless it traps it, so without this every
# `docker compose restart` waited out the full grace period and was then
# SIGKILLed. A cycle interrupted here is safe by construction: an unfinished
# dump is only ever a `.partial`, and an unverified upload never gets its
# marker, so the next cycle simply redoes the work.
child=""
stop() {
	log backup_service_stopping "signal received"
	[ -n "$child" ] && kill -TERM "$child" 2>/dev/null || true
	exit 143
}
trap stop TERM INT

log backup_service_started "interval=${INTERVAL}s retry=${RETRY}s"

flags=""
while true; do
	errs-offsite run-once $flags &
	child=$!
	if wait "$child"; then
		child=""
		flags="--if-due"
		pause="$INTERVAL"
	else
		code=$?
		child=""
		flags="--if-due"
		if [ "$code" -eq 75 ]; then
			# Another cycle (a manual run) holds the lock; it will record
			# its own outcome. Look again soon.
			pause=60
		else
			log backup_will_retry "cycle failed (exit $code); next attempt in ${RETRY}s"
			pause="$RETRY"
		fi
	fi
	sleep "$pause" &
	child=$!
	wait "$child" || true
	child=""
done
