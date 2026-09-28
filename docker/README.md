# Docker

This folder documents the container setup; the compose files themselves
live at the repository root so `docker compose` works from anywhere in the
project without `-f` flags.

## Files

- `../docker-compose.yml` — development environment (hot reload, bind mounts, Postgres exposed on `5432`).
- `../docker-compose.prod.yml` — production overlay (builds the `production` Dockerfile target, no bind mounts).
- `../apps/api/Dockerfile` — multi-stage build for the FastAPI service (`development` / `production` targets).
- `../apps/frontend/Dockerfile` — multi-stage build for the Next.js app (`development` / `production` targets).
- `compose.env.example` — variables consumed by `docker-compose.yml` itself (as opposed to `apps/api/.env`, which the API container reads at runtime).
- `compose.env.production.example` — the production equivalent: TLS hostname, database password, backup cadence.
- `Caddyfile` — TLS termination and routing for the production stack. Validated by `caddy validate`.
- `backup/` — the `backup` service: image (FROM `postgres:16-alpine`, so `pg_dump` matches the server), `backup.sh` scheduler loop, `errs_offsite.py` (dump → validate → upload to Backblaze B2 → read-back verify → retention), `restore.sh` (defaults to a NEW database, refuses the live one without `--force-live`, restores from B2 with `--from-offsite`), and its test suite (`scripts/backup-tests.sh`).

## Local development

```bash
cp apps/api/.env.example apps/api/.env
cp docker/compose.env.example .env
docker compose up --build
```

- API: http://localhost:8000 (docs at `/docs`)
- Frontend: http://localhost:3000
- Postgres: `localhost:5432` (user/password/db default to `errs`/`errs`/`errs`)

## Production

Full instructions, verification steps and rollback are in
[`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md); day-to-day operations and
failure playbooks are in [`../docs/RUNBOOK.md`](../docs/RUNBOOK.md).

```bash
cp docker/compose.env.production.example .env   # then edit
cp apps/api/.env.example apps/api/.env          # then edit for production
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The production overlay adds TLS (Caddy + Let's Encrypt), a one-shot
`migrate` service that gates the API, health checks, restart policies,
resource limits, and nightly off-site backups to Backblaze B2. Only Caddy publishes ports — Postgres,
the API and the frontend are reachable on the compose network only.
