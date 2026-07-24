# Emergency Revenue Recovery System (ERRS)

AI-powered after-hours emergency revenue recovery platform for HVAC,
plumbing, and electrical businesses. See
[`PROJECT_CONTEXT.md`](./PROJECT_CONTEXT.md),
[`MASTER_PROJECT_VISION.docx`](./MASTER_PROJECT_VISION.docx),
[`ARCHITECTURE.md`](./ARCHITECTURE.md), and [`ROADMAP.md`](./ROADMAP.md) for
the product vision and long-term plan.

**This milestone (Milestone 1 — SaaS Foundation) delivers the production
skeleton only:** repository structure, authentication, RBAC scaffolding,
and the frontend shell. No AI, voice, dispatch, CRM, or analytics features
are implemented yet — those are future milestones.

## Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js (App Router), React, TypeScript, Tailwind CSS, Framer Motion, React Hook Form + Zod |
| Backend | FastAPI, Python, SQLAlchemy 2.0 (async), Alembic, PostgreSQL, Pydantic |
| Auth | JWT access tokens + revocable refresh tokens, bcrypt |
| Infra | Docker, Docker Compose |

## Repository structure

```
apps/
  api/            FastAPI backend — Clean Architecture (see below)
  frontend/       Next.js frontend
docs/             Setup and migration guides
docker/           Docker documentation and compose env template
scripts/          Local dev helper scripts
docker-compose.yml
docker-compose.prod.yml
```

### Backend layers (`apps/api/app`)

```
api/            FastAPI routers + request-scoped dependencies (auth, DB session)
application/    Services (use-case orchestration) and Pydantic DTOs
domain/         Entities, repository interfaces, domain exceptions — no framework or DB imports
infrastructure/ SQLAlchemy models, repository implementations, JWT/password/token utilities
core/           Settings, logging setup, middleware, centralized error handling
shared/         Cross-cutting utilities (logging, slugify, ...)
```

Dependency direction is strict: `api` → `application` → `domain`, with
`infrastructure` implementing `domain` interfaces and being wired in at the
`api` layer via dependency injection (`app/api/deps.py`). `domain` never
imports from `infrastructure` or `api`.

### Frontend layers (`apps/frontend/src`)

```
app/            Routes: (auth) route group (login/register), (dashboard) route group (dashboard/settings)
components/ui/  Reusable design-system primitives (Button, Input, Card, Table, Dialog, Modal, Form, Badge, Toast, Skeleton, EmptyState)
components/layout/    Sidebar, top nav, mobile nav, dashboard shell
components/providers/ Auth context, toast context
lib/api/        Typed API client with automatic 401 → refresh → retry
hooks/          useAuth, useToast
middleware.ts   Edge-level route protection based on session cookie presence
```

## Getting started

```bash
./scripts/bootstrap.sh
docker compose up --build
docker compose exec api alembic upgrade head
```

Then visit http://localhost:3000. See
[`docs/LOCAL_SETUP.md`](./docs/LOCAL_SETUP.md) for the full guide (including
running each app outside Docker) and
[`docs/DATABASE_MIGRATIONS.md`](./docs/DATABASE_MIGRATIONS.md) for the
migration workflow.

## Authentication model

- **Access tokens** are short-lived JWTs (default 15 min) returned in the
  response body and held in memory on the client — never in
  `localStorage`, so an XSS payload can't read them off disk.
- **Refresh tokens** are opaque, high-entropy strings, stored server-side
  as a salted hash (so a database leak doesn't expose usable tokens) and
  set as an `httpOnly`, `SameSite=Lax` cookie scoped to `/api/v1/auth`.
  They're rotated on every use and revocable (logout, or in future work,
  reuse detection / password change).
- RBAC is seeded per-organization on registration (`Owner`, `Admin`,
  `Member` roles); permissions are checked via
  `require_permission("users:manage")`-style FastAPI dependencies.

## Testing

```bash
docker compose exec api pytest
```

Unit tests need no database. `tests/integration/test_auth_flow.py`
exercises the full register → login → refresh → logout flow against a
real Postgres database and skips itself if one isn't reachable.
