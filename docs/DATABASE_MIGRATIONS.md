# Database Migrations

Migrations are managed with Alembic and live in `apps/api/alembic/versions`.
`app/infrastructure/database/models/__init__.py` imports every ORM model so
`Base.metadata` (and therefore autogenerate) always sees the full schema.

## Creating a migration

After adding or changing a model in `app/infrastructure/database/models/`:

```bash
docker compose exec api alembic revision --autogenerate -m "add refresh_tokens table"
```

Or, without Docker (with `DATABASE_URL` pointed at a real Postgres instance):

```bash
cd apps/api
alembic revision --autogenerate -m "add refresh_tokens table"
```

**Always read the generated migration before applying it.** Autogenerate
diffs the models against the live database schema — it's a good first
draft, not a guarantee. It will not detect: column renames (it will see a
drop + an add and lose the data), changes to server-side check constraints
in some dialects, or data migrations (backfilling a new NOT NULL column,
for example) — those must be written by hand in the same revision.

## Applying migrations

```bash
docker compose exec api alembic upgrade head
```

## Rolling back

```bash
docker compose exec api alembic downgrade -1
```

## Checking current state

```bash
docker compose exec api alembic current
docker compose exec api alembic history
```

## Conventions

- One logical change per migration — don't bundle unrelated schema changes.
- Every model change ships with its migration in the same commit.
- Never edit a migration that has already been applied to a shared
  environment (staging/production) — write a new one instead.
