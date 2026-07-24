# Local Setup

## Prerequisites

- Docker Desktop (or Docker Engine + Compose v2.24+)
- Node.js 20+ and Python 3.12 only if you want to run either app **outside**
  Docker; otherwise Docker is the only prerequisite.

## Quickest path: Docker Compose

```bash
cp apps/api/.env.example apps/api/.env
cp docker/compose.env.example .env
docker compose up --build
```

Then open:

- Frontend: http://localhost:3000
- API docs (Swagger UI): http://localhost:8000/docs
- API health check: http://localhost:8000/api/v1/health

The first request to `/` will redirect to `/login` — register an account,
which creates your organization and signs you in.

Alembic migrations do **not** run automatically on container start (a
migration failure should stop a deploy, not run silently) — apply them
once the containers are up:

```bash
docker compose exec api alembic upgrade head
```

See [DATABASE_MIGRATIONS.md](./DATABASE_MIGRATIONS.md) for the full migration workflow.

## Running the API without Docker

```bash
cd apps/api
python -m venv .venv
source .venv/bin/activate  # .venv\Scripts\activate on Windows
pip install -r requirements-dev.txt
cp .env.example .env  # then point DATABASE_URL at a local Postgres instance
alembic upgrade head
uvicorn app.main:app --reload
```

Run the test suite with:

```bash
pytest
```

Unit tests (`tests/unit`) require no database. Integration tests
(`tests/integration/test_auth_flow.py`) need a reachable Postgres database
and will skip themselves automatically if one isn't available.

## Running the frontend without Docker

```bash
cd apps/frontend
npm install
cp .env.example .env.local
npm run dev
```

## Useful commands

| Command | Description |
|---|---|
| `docker compose up --build` | Start Postgres, API, and frontend with hot reload |
| `docker compose down` | Stop all services |
| `docker compose down -v` | Stop all services and delete the Postgres volume |
| `docker compose exec api alembic upgrade head` | Apply pending migrations |
| `docker compose exec api pytest` | Run backend tests inside the container |
| `docker compose exec frontend npm run lint` | Lint the frontend inside the container |
