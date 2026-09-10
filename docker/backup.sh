#!/bin/sh
# Nightly PostgreSQL backup for the controlled pilot.
#
# Runs inside the same `postgres:16-alpine` image as the server, so `pg_dump`
# and the server are the same version. That is not incidental: a client older
# than the server refuses outright, and a newer one can emit a dump the
# server cannot restore — and both failures surface at the moment the backup
# is actually needed, which is the worst possible time to discover them.
#
# A shell loop rather than cron: one nightly job needs no scheduler, and a
# cron daemon in a container adds a second thing that can be running while
# appearing not to be. The trade-off is that the schedule is relative to
# container start rather than wall-clock — acceptable for a pilot, and noted
# in docs/RUNBOOK.md.
#
# Format is custom (`-Fc`), not plain SQL:
#   - compressed, so a pilot's dumps fit on a small disk
#   - restorable selectively with pg_restore (one table, or schema-only)
#   - version-tolerant in a way a raw psql script is not
#
# Retention is applied on every run, before the dump, so a host that was
# powered off for a fortnight does not accumulate unbounded files the first
# time it comes back.

set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

mkdir -p "$BACKUP_DIR"

log() {
	# Same shape as the application's structured logs, so `docker compose
	# logs` reads consistently across services.
	echo "{\"event\":\"$1\",\"service\":\"backup\",\"detail\":\"${2:-}\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
}

take_backup() {
	stamp="$(date -u +%Y%m%dT%H%M%SZ)"
	target="$BACKUP_DIR/errs-${stamp}.dump"
	partial="${target}.partial"

	# Written to `.partial` and renamed only on success. A dump interrupted
	# by a container restart or a full disk would otherwise sit in the
	# directory looking exactly like a good one — and would be picked as
	# "the latest backup" during a real restore.
	if pg_dump --format=custom --compress=6 --file="$partial" 2>/tmp/pg_dump.err; then
		mv "$partial" "$target"
		size="$(du -h "$target" | cut -f1)"
		log backup_completed "$(basename "$target") ($size)"
	else
		rm -f "$partial"
		# The error text can name the host and database but not credentials
		# (PGPASSWORD is never echoed by pg_dump), so it is safe to surface
		# and is the only way an operator learns why backups stopped.
		log backup_failed "$(tr -d '\n' </tmp/pg_dump.err | head -c 300)"
		return 1
	fi
}

prune() {
	# -mtime is whole days, which is what a retention policy expressed in
	# days should mean. Failures are logged rather than fatal: a pruning
	# problem must never stop tonight's backup from being taken.
	deleted="$(find "$BACKUP_DIR" -maxdepth 1 -name 'errs-*.dump' -type f -mtime "+${RETENTION_DAYS}" -print -delete 2>/dev/null | wc -l | tr -d ' ')"
	if [ "$deleted" != "0" ]; then
		log backup_pruned "removed $deleted dump(s) older than ${RETENTION_DAYS}d"
	fi
	# Also clear abandoned partials, which can only be the remains of a run
	# killed mid-dump.
	find "$BACKUP_DIR" -maxdepth 1 -name '*.partial' -type f -mtime +1 -delete 2>/dev/null || true
}

log backup_service_started "interval=${INTERVAL}s retention=${RETENTION_DAYS}d dir=${BACKUP_DIR}"

# One backup immediately on start, then on the interval. Taking the first one
# straight away means a fresh deployment has a restorable dump within seconds
# rather than being unprotected until the first midnight — and it surfaces a
# misconfiguration (wrong password, unreachable host) at deploy time instead
# of silently a day later.
while true; do
	prune
	take_backup || log backup_will_retry "next attempt in ${INTERVAL}s"
	sleep "$INTERVAL"
done
