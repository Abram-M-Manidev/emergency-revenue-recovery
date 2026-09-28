#!/usr/bin/env bash
# Disaster-recovery drill: prove a PostgreSQL backup can be restored from
# OFF-SITE storage onto a machine that has nothing but the backup.
#
#   scripts/backup-restore-drill.sh              # real B2 (B2_* from the root .env)
#   scripts/backup-restore-drill.sh --local-s3   # throwaway Object-Lock S3 server
#                                                # instead of B2 (no credentials)
#
# What it does, in order — each step must pass or the drill stops:
#
#   1. pg_dump the running dev/pilot database (read-only; no downtime) and
#      upload it off-site under restore-drill/<run>/ with a 1-day lock, so a
#      drill never mixes with, or is mistaken for, a production backup.
#   2. Confirm the object exists remotely.
#   3. Start a brand-new PostgreSQL 16 container on an isolated network, with
#      an empty backups volume — the "new VM" with nothing on it.
#   4. From that isolated side, download the backup from off-site storage
#      (sha256-verified) and restore it with the production restore script.
#   5. Compare source and restored: tables, per-table row counts, a digest of
#      every row of every table, foreign keys (all present and validated),
#      indexes, sequences.
#   6. Run Alembic against the restored database: same revision as the
#      source, and `alembic check` (models match schema).
#   7. Start the real API against the restored database: /health/ready, and
#      load every ORM-mapped model through SQLAlchemy.
#   8. Tear everything down (trap on exit, pass or fail).
#
# It never writes to the source database and never restores over anything:
# the only database it creates lives in a container it deletes afterwards.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

MODE=b2
[[ "${1:-}" == "--local-s3" ]] && MODE=local

RUN="drill-$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 3)"
NET="errs-${RUN}"
FRESH_PG="errs-${RUN}-pg"
S3_NAME="errs-${RUN}-s3"
VOL_SRC="errs-${RUN}-src"
VOL_NEW="errs-${RUN}-new"
IMAGE="errs-backup:drill"
WORK="$(mktemp -d)"
say() { printf '\n=== %s\n' "$*"; }
fail() { printf '\nDRILL FAILED: %s\n' "$*" >&2; exit 1; }

# Values are read into this process only — never echoed, never on a command
# line (docker gets `-e NAME`, which copies from this environment).
read_env_file() {  # read_env_file FILE NAME_REGEX — exports matching NAME=value lines
  [[ -f "$1" ]] || return 0
  while IFS='=' read -r name value; do
    if [[ "$name" =~ ^($2)$ ]]; then export "$name=$value"; fi
  done < <(grep -E "^($2)=" "$1" || true)
  return 0
}
read_env_file .env 'POSTGRES_USER|POSTGRES_PASSWORD|POSTGRES_DB|B2_[A-Z0-9_]+'
export POSTGRES_USER="${POSTGRES_USER:-errs}" POSTGRES_DB="${POSTGRES_DB:-errs}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-errs}"

cleanup() {
  say "teardown"
  docker rm -f "$FRESH_PG" "$S3_NAME" "errs-${RUN}-api" >/dev/null 2>&1 || true
  docker volume rm -f "$VOL_SRC" "$VOL_NEW" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
  echo "removed containers, volumes and network for $RUN"
}
trap cleanup EXIT

SRC_PG_ID="$(docker compose ps -q postgres)"
[[ -n "$SRC_PG_ID" ]] || fail "the compose 'postgres' service is not running"
SRC_NET="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$SRC_PG_ID" | awk '{print $1}')"
# The image the running `api` service was created from (dev or production
# target); the current source is mounted over it below either way.
API_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$(docker compose ps -q api)" 2>/dev/null || true)"
[[ -n "$API_IMAGE" ]] || fail "the compose 'api' service is not running"

say "build backup image"
docker build -q --target runtime -t "$IMAGE" docker/backup >/dev/null
docker network create --internal=false "$NET" >/dev/null

export BACKUP_REMOTE_PREFIX="restore-drill/${RUN}"
export BACKUP_OBJECT_LOCK_DAYS=1
if [[ "$MODE" == local ]]; then
  export B2_APPLICATION_KEY_ID="drill$(openssl rand -hex 8)" B2_APPLICATION_KEY="$(openssl rand -hex 24)"
  export B2_BUCKET="errs-drill" B2_S3_ENDPOINT="http://${S3_NAME}:7070" BACKUP_SSE=""
  docker run -d --name "$S3_NAME" --network "$NET" \
    -e ROOT_ACCESS_KEY="$B2_APPLICATION_KEY_ID" -e ROOT_SECRET_KEY="$B2_APPLICATION_KEY" \
    --tmpfs /data --tmpfs /vers --entrypoint sh versity/versitygw:v1.8.0 \
    -c 'exec versitygw posix --versioning-dir /vers /data' >/dev/null
  docker network connect "$SRC_NET" "$S3_NAME"
  sleep 2
  docker run --rm --network "$NET" -e B2_APPLICATION_KEY_ID -e B2_APPLICATION_KEY -e B2_S3_ENDPOINT \
    --entrypoint /opt/offsite/bin/python "$IMAGE" -c "
import os, boto3
boto3.client('s3', endpoint_url=os.environ['B2_S3_ENDPOINT'], region_name='us-east-1',
  aws_access_key_id=os.environ['B2_APPLICATION_KEY_ID'], aws_secret_access_key=os.environ['B2_APPLICATION_KEY']
).create_bucket(Bucket='errs-drill', ObjectLockEnabledForBucket=True)"
  echo "off-site target: throwaway versitygw (Object Lock on) — NOT Backblaze B2"
else
  for n in B2_APPLICATION_KEY_ID B2_APPLICATION_KEY B2_BUCKET; do
    [[ -n "${!n:-}" ]] || fail "$n is not set in the root .env (B2 mode); use --local-s3 to drill without B2"
  done
  export BACKUP_SSE=AES256
  echo "off-site target: Backblaze B2 bucket ${B2_BUCKET}, prefix ${BACKUP_REMOTE_PREFIX}/"
fi
B2_ENV=(-e B2_APPLICATION_KEY_ID -e B2_APPLICATION_KEY -e B2_BUCKET -e B2_S3_ENDPOINT
        -e BACKUP_REMOTE_PREFIX -e BACKUP_OBJECT_LOCK_DAYS -e BACKUP_SSE)

# ---------------------------------------------------------------- 1. backup
say "1. dump the source database and upload it off-site"
export PGPASSWORD="$POSTGRES_PASSWORD"
docker run --rm --network "$SRC_NET" -v "$VOL_SRC:/backups" "${B2_ENV[@]}" \
  -e PGHOST=postgres -e PGUSER="$POSTGRES_USER" -e PGPASSWORD -e PGDATABASE="$POSTGRES_DB" \
  --entrypoint errs-offsite "$IMAGE" run-once || fail "backup cycle did not succeed"

# ----------------------------------------------------------- 2. remote exists
say "2. confirm the object exists off-site"
docker run --rm --network "$NET" "${B2_ENV[@]}" -e PGDATABASE="$POSTGRES_DB" \
  --entrypoint errs-offsite "$IMAGE" list || fail "no off-site backup listed"

# ------------------------------------------------------------ 3. fresh server
say "3. start a fresh, isolated PostgreSQL 16 (the replacement VM)"
export FRESH_PASSWORD="$(openssl rand -hex 16)"
docker run -d --name "$FRESH_PG" --network "$NET" --tmpfs /var/lib/postgresql/data \
  -e POSTGRES_USER="$POSTGRES_USER" -e POSTGRES_PASSWORD="$FRESH_PASSWORD" -e POSTGRES_DB="$POSTGRES_DB" \
  postgres:16-alpine >/dev/null
for _ in $(seq 60); do
  docker exec "$FRESH_PG" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1 && break
  sleep 1
done
# postgres:16 reports ready once during init before restarting; wait for the real one.
sleep 3
docker exec "$FRESH_PG" pg_isready -U "$POSTGRES_USER" >/dev/null || fail "fresh server never became ready"
echo "fresh server has databases: $(docker exec "$FRESH_PG" psql -U "$POSTGRES_USER" -Atc \
  "select string_agg(datname, ',') from pg_database where not datistemplate" postgres)"

# ------------------------------------------------- 4. download + restore
say "4. download from off-site (sha256-verified) and restore into errs_restored"
docker run --rm --network "$NET" -v "$VOL_NEW:/backups" "${B2_ENV[@]}" \
  -e PGHOST="$FRESH_PG" -e PGUSER="$POSTGRES_USER" -e PGPASSWORD="$FRESH_PASSWORD" -e PGDATABASE="$POSTGRES_DB" \
  --entrypoint sh "$IMAGE" /usr/local/bin/errs-restore.sh --from-offsite latest --target errs_restored \
  || fail "restore from off-site failed"

# ---------------------------------------------------------------- 5. compare
say "5. compare source and restored database"
COMPARE_SQL="$(cat <<'SQL'
\pset format unaligned
\pset tuples_only on
SELECT 'table ' || t.table_name || ' rows=' ||
  (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', t.table_schema, t.table_name), false, true, '')))[1]::text
  || ' digest=' ||
  coalesce((xpath('/row/d/text()', query_to_xml(format(
    'SELECT md5(string_agg(x::text, %L ORDER BY x::text)) AS d FROM %I.%I x', '|', t.table_schema, t.table_name),
    false, true, '')))[1]::text, 'empty')
FROM information_schema.tables t
WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
ORDER BY t.table_name;
SELECT 'fk ' || conrelid::regclass || ' ' || conname || ' validated=' || convalidated || ' ' || pg_get_constraintdef(oid)
FROM pg_constraint WHERE contype = 'f' ORDER BY conrelid::regclass::text, conname;
SELECT 'index ' || tablename || ' ' || indexname FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1;
SELECT 'enum ' || t.typname || ' ' || string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid GROUP BY t.typname ORDER BY 1;
SELECT 'sequence ' || sequencename || ' ' || coalesce(last_value::text, 'unused') FROM pg_sequences ORDER BY 1;
SQL
)"
export COMPARE_SQL
docker run --rm --network "$SRC_NET" -e PGPASSWORD -e COMPARE_SQL --entrypoint sh "$IMAGE" \
  -c "printf '%s\n' \"\$COMPARE_SQL\" | psql -X -q -h postgres -U '$POSTGRES_USER' -d '$POSTGRES_DB' -v ON_ERROR_STOP=1" \
  > "$WORK/${RUN}-source.txt" || fail "could not fingerprint source"
docker run --rm --network "$NET" -e PGPASSWORD="$FRESH_PASSWORD" -e COMPARE_SQL --entrypoint sh "$IMAGE" \
  -c "printf '%s\n' \"\$COMPARE_SQL\" | psql -X -q -h '$FRESH_PG' -U '$POSTGRES_USER' -d errs_restored -v ON_ERROR_STOP=1" \
  > "$WORK/${RUN}-restored.txt" || fail "could not fingerprint restored database"

tables=$(grep -c '^table ' "$WORK/${RUN}-restored.txt" || true)
fks=$(grep -c '^fk ' "$WORK/${RUN}-restored.txt" || true)
unvalidated=$(grep '^fk ' "$WORK/${RUN}-restored.txt" | grep -c 'validated=false' || true)
rows=$(grep '^table ' "$WORK/${RUN}-restored.txt" | sed -E 's/.* rows=([0-9]+).*/\1/' | awk '{s+=$1} END {print s+0}')
echo "restored: ${tables} tables, ${rows} rows, ${fks} foreign keys (${unvalidated} not validated), $(grep -c '^index ' "$WORK/${RUN}-restored.txt") indexes"
grep '^table ' "$WORK/${RUN}-restored.txt" | sed -E 's/^table ([^ ]+) rows=([0-9]+).*/  \1: \2 rows/'
if ! diff -u "$WORK/${RUN}-source.txt" "$WORK/${RUN}-restored.txt" > "$WORK/${RUN}-diff.txt"; then
  # Table names and counts only — never row contents.
  sed -E 's/ digest=[0-9a-f]+//' "$WORK/${RUN}-diff.txt" | head -40
  fail "restored database differs from the source (schema, row counts or row digests)"
fi
[[ "$tables" -gt 0 && "$unvalidated" -eq 0 ]] || fail "no tables restored, or unvalidated foreign keys"
echo "IDENTICAL: every table's row count and full-row digest, every FK, index, enum and sequence match"

# ---------------------------------------------------------------- 6. alembic
say "6. Alembic against the restored database"
export RESTORED_URL="postgresql+asyncpg://${POSTGRES_USER}:${FRESH_PASSWORD}@${FRESH_PG}:5432/errs_restored"
API_RUN=(docker run --rm --network "$NET" --env-file apps/api/.env -e DATABASE_URL="$RESTORED_URL"
         -v "$(pwd -W 2>/dev/null || pwd)/apps/api/app:/app/app:ro"
         -v "$(pwd -W 2>/dev/null || pwd)/apps/api/alembic:/app/alembic:ro"
         -v "$(pwd -W 2>/dev/null || pwd)/apps/api/alembic.ini:/app/alembic.ini:ro" "$API_IMAGE")
restored_rev="$("${API_RUN[@]}" alembic current 2>/dev/null | tail -n1)"
source_rev="$(docker compose exec -T api alembic current 2>/dev/null | tail -n1)"
head_rev="$("${API_RUN[@]}" alembic heads 2>/dev/null | tail -n1)"
echo "restored: ${restored_rev} | source: ${source_rev} | code head: ${head_rev}"
[[ -n "$restored_rev" && "$restored_rev" == "$source_rev" ]] || fail "restored Alembic revision differs from source"
[[ "$restored_rev" == *"(head)"* ]] || fail "restored database is not at the code's head revision"
"${API_RUN[@]}" alembic check 2>&1 | tail -n1 | tee "$WORK/${RUN}-check.txt"
grep -q "No new upgrade operations detected" "$WORK/${RUN}-check.txt" || fail "alembic check: models and restored schema disagree"

# ---------------------------------------------------------------- 7. the app
say "7. the API against the restored database"
"${API_RUN[@]}" python -c '
import asyncio
from sqlalchemy import func, select
from app.infrastructure.database import models  # noqa: F401 - registers every mapper
from app.infrastructure.database.session import AsyncSessionLocal, Base

async def main() -> None:
    async with AsyncSessionLocal() as session:
        loaded = 0
        for mapper in sorted(Base.registry.mappers, key=lambda m: m.class_.__name__):
            # Selecting the full entity reads every mapped column: a column the
            # model expects but the restored schema lacks fails right here.
            await session.execute(select(mapper.class_).limit(25))
            count = await session.scalar(select(func.count()).select_from(mapper.class_))
            print(f"  ORM {mapper.class_.__name__}: {count} rows readable")
            loaded += 1
        print(f"ORM OK: {loaded} mapped models loaded from the restored database")

asyncio.run(main())
' || fail "the application could not read the restored database through its ORM"

docker run -d --name "errs-${RUN}-api" --network "$NET" --env-file apps/api/.env -e DATABASE_URL="$RESTORED_URL" \
  -v "$(pwd -W 2>/dev/null || pwd)/apps/api/app:/app/app:ro" "$API_IMAGE" \
  uvicorn app.main:app --host 0.0.0.0 --port 8000 >/dev/null
ready=""
for _ in $(seq 40); do
  ready="$(docker exec "errs-${RUN}-api" python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/v1/health/ready', timeout=3).read().decode())" 2>/dev/null || true)"
  [[ "$ready" == *ready* ]] && break
  sleep 1
done
echo "GET /api/v1/health/ready -> ${ready:-no response}"
[[ "$ready" == *'"ready"'* ]] || fail "API did not report ready against the restored database"

say "DRILL PASSED ($MODE): off-site backup ${BACKUP_REMOTE_PREFIX}/ restored onto a fresh server and verified"
