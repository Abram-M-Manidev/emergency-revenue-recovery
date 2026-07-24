#!/usr/bin/env bash
# One-time local setup: creates the env files Docker Compose and the API need.
# Run from the repository root: ./scripts/bootstrap.sh
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

copy_if_missing() {
  local src="$1" dest="$2"
  if [ -f "$dest" ]; then
    echo "skip: $dest already exists"
  else
    cp "$src" "$dest"
    echo "created: $dest"
  fi
}

copy_if_missing "$repo_root/apps/api/.env.example" "$repo_root/apps/api/.env"
copy_if_missing "$repo_root/apps/frontend/.env.example" "$repo_root/apps/frontend/.env.local"
copy_if_missing "$repo_root/docker/compose.env.example" "$repo_root/.env"

echo
echo "Done. Next steps:"
echo "  1. Review apps/api/.env and set a real JWT_SECRET_KEY for anything beyond local dev."
echo "  2. docker compose up --build"
echo "  3. docker compose exec api alembic upgrade head"
