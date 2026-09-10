# ESSR pilot operations runbook

What to do when something needs doing, or has gone wrong. Written for the
single-VM pilot deployment in `docs/DEPLOYMENT.md`.

Every command assumes you are in the repository root on the deployment host.
The compose invocation is long, so set this once per shell:

```bash
alias essr='docker compose -f docker-compose.yml -f docker-compose.prod.yml'
```

---

## Everyday commands

| Task | Command |
|---|---|
| Start everything | `essr up -d` |
| Stop everything | `essr stop` (keeps data) |
| Restart one service | `essr restart api` |
| What is running | `essr ps` |
| Follow logs | `essr logs -f api` |
| Logs since a time | `essr logs --since 30m api` |
| Database shell | `essr exec postgres psql -U errs -d errs` |
| Current schema revision | `essr exec api alembic current` |
| Apply migrations | `essr run --rm migrate` |

`essr down` also removes containers. It does **not** delete named volumes, so
your data and certificates survive — but never add `-v`, which does delete
them.

---

## Is ESSR healthy?

```bash
essr ps                                    # every service Up; api/frontend healthy
curl -sS https://YOUR_DOMAIN/api/v1/health        # process alive
curl -sS https://YOUR_DOMAIN/api/v1/health/ready  # alive AND database reachable
```

`/health` answers from the process alone. `/health/ready` runs `SELECT 1`, so
it is the one that distinguishes "the API is up" from "the API can actually
serve a request". The container healthcheck uses `/health/ready` for exactly
that reason.

### Structured log events worth knowing

Logs are JSON. These are the events that answer the operational questions:

```bash
# Are voice calls arriving and being routed to the right tenant?
essr logs --since 1h api | grep -E 'voice_request_received|voice_line_resolved'

# Are AI calls failing?
essr logs --since 1h api | grep -E 'ai_provider_|vapi_chat_completion_.*error'

# Are tools (booking, tickets) failing?
essr logs --since 1h api | grep voice_tool_executed | grep '"success":false'

# Are emergency alerts being delivered?
essr logs --since 24h api | grep emergency_notification_attempted

# Alerts that did NOT reach a human — the highest-priority line in this file
essr logs --since 24h api | grep emergency_notification_attempted \
  | grep -v '"alerted_a_human":true'

# Is one tenant failing repeatedly? (group by organization_id)
essr logs --since 1h api | grep '"success":false' \
  | grep -o '"organization_id":"[^"]*"' | sort | uniq -c | sort -rn
```

Logs never contain phone numbers, webhook URLs, API keys, passwords, refresh
tokens, or caller addresses. If you find one, that is a bug worth fixing
immediately — the notification destination in particular is a credential.

---

## Emergency alerts: checking what actually reached a human

The single most important operational question in this system. A ticket
existing is not the same as someone being told.

```bash
essr exec postgres psql -U errs -d errs -c "
  SELECT o.name,
         d.status,
         d.attempts,
         d.error_code,
         d.created_at
    FROM emergency_notification_deliveries d
    JOIN organizations o ON o.id = d.organization_id
   WHERE d.created_at > now() - interval '7 days'
   ORDER BY d.created_at DESC;"
```

| `status` | Meaning | Action |
|---|---|---|
| `delivered` | A provider accepted the alert. The assistant was allowed to tell the caller a dispatcher was alerted. | none |
| `failed` | We tried and the endpoint refused, timed out, or was unreachable. The caller was told the alert could **not** be confirmed. | Check `error_code`; verify the business's webhook still works |
| `not_configured` | That business has no destination set (or has paused it). | Onboard them — Settings → Emergency alerts |
| `pending` | In flight, or a request died mid-attempt. | If it persists, check API logs for that `ticket_id` |

`error_code` is a short token (`timeout`, `http_500`, `transport_error`,
`no_destination_configured`) and never a response body, because provider
responses can echo the webhook URL back.

---

## Disabling one tenant's voice assistant

When a business's assistant is misbehaving and you need it to stop **now**.

**Preferred — the dashboard:** sign in as that organization's Owner →
**Settings → Voice assistant → Disable**. Takes effect on the next call; the
flag is read per call and cached nowhere.

**If the dashboard is unavailable:**

```bash
essr exec postgres psql -U errs -d errs -c "
  UPDATE organizations SET voice_assistant_enabled = false, updated_at = now()
   WHERE name = 'THE BUSINESS';"
```

Callers then hear that the automated line is unavailable and are asked to
hold for the team; the call ends and nothing is recorded from it.

**This is not the same as deactivating the organization.** `is_active = false`
locks every teammate out of the dashboard — including the people you need to
investigate with. Use the voice switch unless you genuinely mean to suspend
the account.

Re-enable by setting it back to `true`.

To confirm which tenants are currently switched off:

```bash
essr exec postgres psql -U errs -d errs -c "
  SELECT name, is_active, voice_assistant_enabled FROM organizations
   WHERE NOT voice_assistant_enabled OR NOT is_active;"
```

---

## Backups

Taken by the `backup` service: one immediately on start, then every
`BACKUP_INTERVAL_SECONDS` (default nightly), pruned after
`BACKUP_RETENTION_DAYS` (default 14). Format is `pg_dump -Fc`, so
`pg_restore` can restore selectively.

```bash
# what exists
essr exec backup sh /usr/local/bin/errs-restore.sh --list

# force one right now (before a risky change)
essr exec backup sh -c 'BACKUP_INTERVAL_SECONDS=1 timeout 120 sh /usr/local/bin/errs-backup.sh'

# is the backup service healthy?
essr logs --since 48h backup | grep -E 'backup_completed|backup_failed'
```

> **Known limitation, stated plainly:** dumps live in the `postgres_backups`
> Docker volume **on the same host as the database**. They survive a
> container rebuild, an image change and a `docker compose down`. They do
> **not** survive losing the host. Copying them off-host is deliberately
> deferred — do not describe these backups as disaster recovery until that
> exists.

### Restore a backup

The order matters. Restore into a **copy**, verify the copy, and only then
swap. A restore straight over the live database destroys the very data you
would need if the backup turned out to be bad.

```bash
# 1. RESTORE into a new database (live is untouched; the script refuses
#    --target errs without an explicit --force-live)
essr exec backup sh /usr/local/bin/errs-restore.sh --latest --target errs_restored

# 2. VERIFY — the script prints schema revision and row counts. Compare them
#    against what you expect. Optionally point the API at the copy:
#      DATABASE_URL=postgresql+asyncpg://errs:PASS@postgres:5432/errs_restored
#    and exercise the dashboard before committing to it.

# 3. SWAP, only once satisfied
essr stop api
essr exec postgres psql -U errs -d postgres \
  -c 'ALTER DATABASE "errs" RENAME TO "errs_broken";' \
  -c 'ALTER DATABASE "errs_restored" RENAME TO "errs";'
essr start api

# 4. RETURN TO NORMAL
curl -sS https://YOUR_DOMAIN/api/v1/health/ready
essr exec api alembic current
```

Keep `errs_broken` until you are certain. It is the only copy of whatever the
backup did not contain.

`DROP`/`RENAME DATABASE` fails while anything holds a connection — that is
why `api` is stopped first, and the failure is a useful guard rather than an
obstacle.

---

## Failure playbooks

### The site is down

```bash
essr ps                      # which service is not Up?
essr logs --tail 100 <svc>
```

- **`api` restarting in a loop** → almost always configuration. The entrypoint
  validates `Settings()` before uvicorn starts, so the reason is in the first
  few log lines (`ValueError: ... must be set when ENVIRONMENT=production`).
  Fix `apps/api/.env`, then `essr up -d api`.
- **`migrate` exited non-zero** → the API deliberately never started. Read
  `essr logs migrate`, fix, re-run `essr run --rm migrate`.
- **`caddy` up but TLS failing** → see below.

### Certificate problems

```bash
essr logs --since 1h caddy | grep -iE 'certificate|acme|error'
```

Almost always one of: DNS does not point here yet, port 80 is blocked, or
Let's Encrypt rate limits were hit while retrying. Confirm DNS
(`dig +short YOUR_DOMAIN`) and reachability (`curl -I http://YOUR_DOMAIN/`)
before retrying, because failed attempts consume the rate-limit budget.

### The database is down or unreachable

```bash
essr ps postgres
essr logs --tail 100 postgres
essr exec postgres pg_isready -U errs
```

`/health/ready` returns non-200 while this is true, and the API's healthcheck
marks it unhealthy — which is correct: it cannot serve. Restart Postgres
first (`essr restart postgres`); the API reconnects on its own. If the data
directory is corrupt, go to **Restore a backup**.

### The AI provider is failing

Symptom: callers hear "I'm having trouble connecting to our system right
now"; logs show `AIProviderUnavailableError` or repeated provider errors.

```bash
essr logs --since 30m api | grep -iE 'ai_provider|openai'
```

Check OpenAI's status page and the account's quota. There is no local
fallback model by design — the assistant says something truthful and the call
ends, rather than improvising. If the outage is prolonged, disable the voice
assistant for affected tenants (above) so callers get the "hold for our team"
message instead of an error every time.

### Vapi is failing / calls never arrive

```bash
essr logs --since 1h api | grep voice_request_received     # nothing → not reaching us
essr logs --since 1h caddy | grep '/api/v1/voice'          # nothing → not reaching Caddy
```

- Requests reaching Caddy but rejected by the API (`401`) → `VAPI_SERVER_SECRET`
  does not match the assistant's Server URL Secret.
- Reaching the API but `voice_line_not_found` → no `voice_lines` row maps that
  assistant id to an organization (see `docs/DEPLOYMENT.md` step 6).
- Nothing at Caddy at all → the Vapi assistant's Server URL is wrong, or DNS
  or TLS is broken.

**Changing Vapi configuration is an external action.** It is not managed by
this repository and is not covered by any rollback here.

---

## Rotating secrets

Rotate one at a time and verify between each.

| Secret | Procedure | Blast radius |
|---|---|---|
| `JWT_SECRET_KEY` | Edit `apps/api/.env`, `essr up -d api` | **Every user is signed out.** All access and refresh tokens become invalid. Do it deliberately. |
| `VAPI_SERVER_SECRET` | Update the Vapi assistant's Server URL Secret **first**, then `apps/api/.env`, then `essr up -d api` | Calls fail while the two disagree — keep the gap short |
| `POSTGRES_PASSWORD` | `ALTER USER errs WITH PASSWORD '…';` then update `.env` **and** `apps/api/.env`, then `essr up -d` | API and backup service both need the new value |
| `OPENAI_API_KEY` | Edit `apps/api/.env`, `essr up -d api` | Calls in flight fail; new ones use the new key |
| A tenant's webhook URL | Dashboard → Settings → Emergency alerts → paste the new one (replaces) | That tenant's alerts only |

Never commit any of these. `.gitignore` excludes `.env` files; `apps/api/.env.example`
and `docker/compose.env.production.example` are the templates.

---

## What this deployment does *not* yet do

Stated so nobody discovers it during an incident:

- **Backups do not survive host loss.** Same-host volume only.
- **No alerting.** Nothing pages you when the API goes down, a migration
  fails, or emergency notifications start failing. You find out by looking.
  Checking the `emergency_notification_attempted` query above daily is the
  minimum viable substitute during a pilot.
- **No zero-downtime deploys.** A deploy drops in-flight calls for a few
  seconds.
- **Rate limits are per worker**, so the effective ceiling is roughly 4× the
  configured value.
- **No data-deletion / retention tooling.** Transcripts and customer records
  accumulate indefinitely; there is no self-serve erasure path yet.
- **Voice line provisioning is manual** — a SQL insert, no UI.

These are Phase 3 items, not oversights.
