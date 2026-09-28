#!/usr/bin/env bash
# Run the backup test suite: real PostgreSQL 16, a real Object-Lock S3 server,
# and (with --live) the real B2 bucket.
#
#   scripts/backup-tests.sh            # local S3 server only
#   scripts/backup-tests.sh --live     # also the real B2 bucket, using B2_*
#                                      # from the root .env (never printed)
#   scripts/backup-tests.sh -k retention   # extra args go to pytest
#
# Credentials for the throwaway Postgres and S3 server are random per run and
# exist only in this process's environment.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1  # Git Bash on Windows: do not rewrite container paths

compose=(docker compose -f docker/backup/compose.test.yml)
export TEST_PG_PASSWORD="$(openssl rand -hex 16)"
export TEST_S3_ACCESS="test$(openssl rand -hex 8)"
export TEST_S3_SECRET="$(openssl rand -hex 24)"
export ERRS_B2_LIVE=0

if [[ "${1:-}" == "--live" ]]; then
  shift
  export ERRS_B2_LIVE=1
  # Read only the B2_* lines, without `source`-ing the whole file or echoing it.
  if [[ -f .env ]]; then
    while IFS='=' read -r name value; do
      if [[ "$name" =~ ^B2_[A-Z0-9_]+$ ]]; then export "$name=$value"; fi
    done < <(grep -E '^B2_[A-Z0-9_]+=' .env || true)
  fi
  for name in B2_APPLICATION_KEY_ID B2_APPLICATION_KEY B2_BUCKET; do
    if [[ -z "${!name:-}" ]]; then
      echo "--live needs $name (set it in the root .env)" >&2
      exit 2
    fi
  done
fi

cleanup() { "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

"${compose[@]}" build tests
"${compose[@]}" run --rm tests python -m pytest -q "$@"
"${compose[@]}" run --rm tests sh -c 'ruff check . && mypy errs_offsite.py'
