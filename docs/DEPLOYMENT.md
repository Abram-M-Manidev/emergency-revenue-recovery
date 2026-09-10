# Deploying ESSR for a controlled pilot

The smallest production setup that a real service business can rely on.
Deliberately not Kubernetes: a pilot serves a handful of businesses on one
phone line each, and an orchestrator would add real failure modes for no
benefit at that size. Everything here is upgradeable in place — see
[Growing out of this](#growing-out-of-this).

---

## What you are deploying

```
                        Internet
                            │
                     ┌──────┴──────┐
                     │    caddy    │  :80 → :443 redirect
                     │  TLS (ACME) │  automatic Let's Encrypt for ERRS_DOMAIN
                     └──┬───────┬──┘
              /api/*    │       │   everything else
                        ▼       ▼
                  ┌─────────┐ ┌──────────┐
                  │   api   │ │ frontend │
                  │ uvicorn │ │  Next.js │
                  │ 4 works │ │standalone│
                  └────┬────┘ └──────────┘
                       │
                  ┌────▼─────┐        ┌──────────┐
                  │ postgres │◀───────│  backup  │  nightly pg_dump
                  │ internal │        │          │  → postgres_backups
                  │   only   │        └──────────┘
                  └────▲─────┘
                       │ runs once, before api starts
                  ┌────┴─────┐
                  │ migrate  │  alembic upgrade head
                  └──────────┘

External services the API talks out to:
  OpenAI  ──  the AI Brain (required)
  Vapi    ──  inbound voice webhooks (required for phone calls)
  Webhook ──  per-tenant emergency alert destination (optional, per business)
```

Only Caddy publishes ports. Postgres, the API and the frontend are reachable
only on the compose network, so there is no way to bypass TLS by hitting the
host's IP and no database listening on a public interface.

---

## Host requirements

| | |
|---|---|
| OS | Any Linux with Docker Engine 24+ and Compose v2.24+ (the `!override`/`!reset` merge tags are used) |
| Size | 2 vCPU / 4 GB RAM / 40 GB disk is comfortable for a pilot |
| Ports | 80 and 443 open inbound from the internet |
| DNS | `ERRS_DOMAIN` resolving to the host's public IP **before** first boot |

Port 80 is not optional: Let's Encrypt proves control of the domain by
fetching a token over it. A firewall that blocks 80 fails certificate
issuance in a confusing loop.

---

## First deployment

### 1. Get the code onto the host

```bash
git clone https://github.com/Abram-M-Manidev/emergency-revenue-recovery.git
cd emergency-revenue-recovery
git checkout main
```

### 2. Configure the compose stack

```bash
cp docker/compose.env.production.example .env
$EDITOR .env
```

Set `ERRS_DOMAIN`, `ERRS_TLS_EMAIL`, and generate `POSTGRES_PASSWORD`:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. Configure the API

```bash
cp apps/api/.env.example apps/api/.env
$EDITOR apps/api/.env
```

Production **will refuse to start** without all of these. Each one fails
closed at runtime in a way that leaves the deployment looking healthy while
dropping real calls, which is why they are startup errors instead:

| Setting | Why it is mandatory in production |
|---|---|
| `ENVIRONMENT=production` | Turns on every check in this table |
| `DEBUG=false` | Tracebacks would reach callers |
| `JWT_SECRET_KEY` | The `.env.example` placeholder is rejected outright |
| `CORS_ORIGINS=https://YOUR_DOMAIN` | Explicit, no `*`, and no cleartext `http://` |
| `VAPI_SERVER_SECRET` | Unset ⇒ every inbound webhook rejected ⇒ the phone line silently answers nothing |
| `OPENAI_API_KEY` | Unset ⇒ every turn fails and the caller hears the fallback sentence |
| `NOTIFICATION_PROVIDER` | `logging` is refused: it reports success while notifying nobody |

Generate the JWT secret and the Vapi shared secret the same way as the
database password. `DATABASE_URL` is set by the compose overlay — leave
whatever is in `apps/api/.env`; the overlay's value wins.

### 4. Bring it up

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Order is enforced by the compose file: `postgres` becomes healthy, then
`migrate` runs `alembic upgrade head` to completion, and only then does `api`
start. If a migration fails the API never starts — the deploy stops visibly
rather than serving traffic against a half-migrated schema.

### 5. Verify before telling anyone it is live

Run all of these. See `docs/RUNBOOK.md` for what to do when one fails.

```bash
# every service Up, api and frontend healthy
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps

# migrations landed on the expected revision
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec api alembic current

# TLS, and the API answering through it
curl -sS https://YOUR_DOMAIN/api/v1/health          # {"status":"ok"}
curl -sS https://YOUR_DOMAIN/api/v1/health/ready    # {"status":"ready"} — proves DB reachability

# certificate is real and not self-signed
curl -sSI https://YOUR_DOMAIN/ | head -1

# the dashboard renders
curl -sS -o /dev/null -w '%{http_code}\n' https://YOUR_DOMAIN/login   # 200

# Postgres is NOT reachable from outside
nc -zv YOUR_DOMAIN 5432    # must fail

# the first backup has already been taken (the service takes one on start)
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec backup sh /usr/local/bin/errs-restore.sh --list
```

### 6. Point Vapi at the stable URL

**This is an external change and is not automated by this repository.**

In the Vapi dashboard, on the assistant serving the pilot business:

- **Server URL / Custom LLM URL** →
  `https://YOUR_DOMAIN/api/v1/voice/vapi/chat/completions`
- **Server URL Secret** → the same value as `VAPI_SERVER_SECRET`

Then confirm the mapping exists on our side. A `voice_lines` row must link
the Vapi assistant id to the organization, and there is currently **no UI to
create one** — insert it directly:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec postgres \
  psql -U errs -d errs -c "
    INSERT INTO voice_lines (id, organization_id, provider, vapi_assistant_id,
                             phone_number, is_active, created_at, updated_at)
    VALUES (gen_random_uuid(),
            (SELECT id FROM organizations WHERE name = 'THE BUSINESS'),
            'VAPI', 'asst_xxxxxxxx', '+15551234567', true, now(), now());"
```

Verify the whole path with a real call to the pilot number, and watch:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  logs -f api | grep -E 'voice_request_received|voice_line_resolved|voice_tool_executed'
```

### 7. Configure each business's emergency alerts

Sign in as that organization's Owner → **Settings → Emergency alerts**, and
paste an HTTPS webhook the team already watches (Slack or Teams incoming
webhook, PagerDuty Events v2, or anything accepting JSON).

Until this is set, ESSR **truthfully tells emergency callers that their
request is logged but that it could not confirm anyone was alerted.** That is
correct behaviour, not a bug — but it is also not what a pilot wants, so
treat this step as part of onboarding rather than optional.

---

## Deploying a new version

```bash
cd emergency-revenue-recovery

# 1. Know what you are on, so you can get back to it
git rev-parse --short HEAD          # ← write this down
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec api alembic current          # ← and this

# 2. Take a backup before any migration runs
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec backup sh -c 'BACKUP_INTERVAL_SECONDS=1 timeout 120 sh /usr/local/bin/errs-backup.sh'

# 3. Fetch and build
git fetch origin && git checkout <new-ref>
docker compose -f docker-compose.yml -f docker-compose.prod.yml build

# 4. Apply. `migrate` runs first; api will not start if it fails.
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# 5. Re-run the step 5 verification above.
```

There is a short window during step 4 where the API restarts and inbound
calls fail. For a pilot that is acceptable; deploy outside the business's
after-hours window. Zero-downtime deploys need a second API replica behind
Caddy and are deliberately out of scope here.

---

## Rolling back

**Application and database rollback are different operations. Do the
application first, and only touch the database if you must.**

### Application rollback (safe, fast, the usual answer)

```bash
git checkout <previous-ref>
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

This works whenever the new release's migrations were **additive** — which
every migration in this repository so far has been (new tables, new nullable
or defaulted columns). Older code simply ignores columns it does not know
about. Check the migration before assuming:

```bash
git show <new-ref> --stat -- apps/api/alembic/versions/
```

### Database rollback (last resort)

Alembic downgrades exist and are tested, but a downgrade **drops columns and
the data in them**. Prefer restoring the pre-deploy backup over downgrading,
because a restore loses only the writes since the backup while a downgrade
loses a column permanently.

If you are certain:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop api
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec migrate alembic downgrade -1
# then deploy the matching older application version
```

Never downgrade with the API running: the old and new schema would be in use
simultaneously.

For restore-based recovery, see the **Restore a backup** section of
`docs/RUNBOOK.md`.

---

## Growing out of this

Each of these is a contained change, not a rewrite:

| Pressure | Change |
|---|---|
| Database needs HA / point-in-time recovery | Delete the `postgres` service, point `DATABASE_URL` at a managed instance. The `backup` service can stay or be replaced by the provider's snapshots. |
| Deploys must be zero-downtime | Add a second `api` replica; Caddy load-balances `reverse_proxy` upstreams already. |
| Backups must survive host loss | Ship `postgres_backups` off-host (S3/B2 sync sidecar). **Today they live on the same host as the database** — see the honest limitation in `docs/RUNBOOK.md`. |
| Rate limits must be shared across workers | Replace the in-process limiter with Redis. Currently each of the 4 workers counts separately, so the real ceiling is ~4× the configured limit. |
| More than one host | This is the point to consider a PaaS or an orchestrator, not before. |
