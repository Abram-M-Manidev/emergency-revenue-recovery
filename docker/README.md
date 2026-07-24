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

## Local development

```bash
cp apps/api/.env.example apps/api/.env
cp docker/compose.env.example .env
docker compose up --build
```

- API: http://localhost:8000 (docs at `/docs`)
- Frontend: http://localhost:3000
- Postgres: `localhost:5432` (user/password/db default to `errs`/`errs`/`errs`)

## Production-style build

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build
```
