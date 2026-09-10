#!/bin/sh
# Restore an ESSR PostgreSQL backup.
#
# Deliberately NOT part of the backup service. Restoring is a decision, not a
# schedule: it overwrites live data, and anything that could do it
# automatically is a way to lose a business's call history to a bug.
#
# Default target is a NEW database, not the live one. That ordering is the
# whole point of this script existing:
#
#     restore into a copy  ->  verify the copy  ->  only then swap
#
# A restore straight over the live database destroys the very data you would
# need if the backup turned out to be bad — and a backup is only known-good
# once something has read it. `--force-live` exists for the case where the
# live database is already gone, and it says so out loud before proceeding.
#
# Usage, from the repository root:
#
#   # list what is available
#   docker compose exec backup sh /usr/local/bin/errs-restore.sh --list
#
#   # restore the newest dump into errs_restored, leaving live untouched
#   docker compose exec backup sh /usr/local/bin/errs-restore.sh --latest
#
#   # restore a specific dump into a named database
#   docker compose exec backup sh /usr/local/bin/errs-restore.sh \
#       --file /backups/errs-20260910T020000Z.dump --target errs_verify
#
# See docs/RUNBOOK.md for the full BACKUP -> RESTORE -> VERIFY -> SWAP
# procedure, including how to point the API at a restored copy.

set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
LIVE_DB="${PGDATABASE:-errs}"
TARGET="errs_restored"
DUMP=""
FORCE_LIVE="no"

usage() {
	cat <<'USAGE'
errs-restore.sh — restore an ESSR backup into a database

  --list                 show available dumps, newest first
  --latest               restore the newest dump
  --file <path>          restore this specific dump
  --target <dbname>      database to restore INTO (default: errs_restored)
  --force-live           allow --target to be the live database
                         (destroys current live data — only when live is
                         already lost, and never as the first attempt)
  -h, --help             this text
USAGE
}

log() {
	echo "{\"event\":\"$1\",\"service\":\"restore\",\"detail\":\"${2:-}\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
}

list_dumps() {
	# Newest first: during an incident the answer to "which one?" is almost
	# always "the most recent that predates the damage", and that is easier
	# to pick from a descending list.
	ls -1t "$BACKUP_DIR"/errs-*.dump 2>/dev/null || true
}

while [ $# -gt 0 ]; do
	case "$1" in
		--list)
			printf '%s\n' "Available dumps in $BACKUP_DIR (newest first):"
			found="$(list_dumps)"
			if [ -z "$found" ]; then
				printf '%s\n' "  (none — check that the backup service is running)"
				exit 1
			fi
			printf '%s\n' "$found" | while read -r f; do
				printf '  %s  %s\n' "$(du -h "$f" | cut -f1)" "$f"
			done
			exit 0
			;;
		--latest) DUMP="$(list_dumps | head -n 1)" ;;
		--file) shift; DUMP="${1:-}" ;;
		--target) shift; TARGET="${1:-}" ;;
		--force-live) FORCE_LIVE="yes" ;;
		-h|--help) usage; exit 0 ;;
		*) printf 'Unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
	esac
	shift
done

if [ -z "$DUMP" ]; then
	printf 'Nothing to restore: pass --latest or --file <path>.\n\n' >&2
	usage >&2
	exit 2
fi

if [ ! -f "$DUMP" ]; then
	log restore_aborted "dump not found: $DUMP"
	exit 1
fi

# The guard that makes the default safe. Restoring over live is possible, but
# only when the operator has said so explicitly — an accidental --target
# typo must not be able to destroy production.
if [ "$TARGET" = "$LIVE_DB" ] && [ "$FORCE_LIVE" != "yes" ]; then
	log restore_aborted "refusing to overwrite the live database '$LIVE_DB' without --force-live"
	cat >&2 <<EOF

Restore into a copy first, then verify it, then swap. Suggested:

    sh /usr/local/bin/errs-restore.sh --file "$DUMP" --target errs_restored

If the live database is already lost and you intend to overwrite it, re-run
with --force-live.
EOF
	exit 1
fi

if [ "$TARGET" = "$LIVE_DB" ]; then
	log restore_overwriting_live "target=$TARGET — current live data will be destroyed"
fi

log restore_started "dump=$(basename "$DUMP") target=$TARGET"

# `DROP DATABASE` fails while anything holds a connection, which during an
# incident is usually the API. That failure is deliberate and useful: it
# stops a restore racing live writers, and the fix is to stop the API first
# (docs/RUNBOOK.md says so at the point it matters).
psql -q -d postgres -v ON_ERROR_STOP=1 \
	-c "DROP DATABASE IF EXISTS \"$TARGET\";" \
	-c "CREATE DATABASE \"$TARGET\" OWNER \"${PGUSER:-errs}\";"

# --no-owner/--no-privileges so a dump taken as one role restores cleanly
# under another. Without them a restore onto a fresh host fails on every
# GRANT for a role that does not exist there yet.
if pg_restore --no-owner --no-privileges --dbname "$TARGET" "$DUMP"; then
	log restore_completed "target=$TARGET"
else
	# pg_restore exits non-zero on warnings as well as errors, so this is
	# reported rather than treated as certain failure — and the verification
	# step below is what actually decides.
	log restore_finished_with_warnings "target=$TARGET — verify before trusting it"
fi

# Verification, printed rather than merely asserted: an operator mid-incident
# needs to see the shape of what came back, and "does this look like our
# data?" is a judgement only they can make.
log restore_verifying "target=$TARGET"
psql -q -d "$TARGET" -c "
    SELECT 'schema_version' AS check, version_num AS value FROM alembic_version
    UNION ALL SELECT 'organizations', count(*)::text FROM organizations
    UNION ALL SELECT 'users', count(*)::text FROM users
    UNION ALL SELECT 'conversations', count(*)::text FROM conversations
    UNION ALL SELECT 'emergency_tickets', count(*)::text FROM emergency_tickets
    UNION ALL SELECT 'appointments', count(*)::text FROM appointments
    UNION ALL SELECT 'customers', count(*)::text FROM customers;
"

cat <<EOF

Restored '$DUMP' into database '$TARGET'.

Next steps (see docs/RUNBOOK.md):
  1. Compare the counts above against what you expect.
  2. Point the API at it temporarily to exercise the app:
       DATABASE_URL=postgresql+asyncpg://USER:PASS@postgres:5432/$TARGET
  3. Only once satisfied, promote it:
       - stop the api service
       - ALTER DATABASE "$LIVE_DB" RENAME TO "${LIVE_DB}_broken";
       - ALTER DATABASE "$TARGET" RENAME TO "$LIVE_DB";
       - start the api service
     Keep '${LIVE_DB}_broken' until you are certain. It is the only copy of
     whatever the backup did not contain.
EOF
