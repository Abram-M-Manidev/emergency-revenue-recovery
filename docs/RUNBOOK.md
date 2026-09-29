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

## External monitoring (required before going live)

Everything in "Is ESSR healthy?" above runs **on the VM**. The container
healthchecks, `essr ps`, the logs, and the backup service's own `unhealthy`
status all stop existing at the exact moment the host dies, loses its
network, or is deleted — so none of them can ever tell you that happened.
Two checks must therefore run **somewhere else**: on an external monitoring
service, with alerts to a channel that does not depend on this VM (phone/SMS
or a mobile push app — an emergency line going dark is an emergency).

Neither is configured by this repository. The operator creates both on a
monitoring service of their choice (uptime monitors and cron/heartbeat
monitors are offered by services such as Healthchecks.io, Better Stack,
UptimeRobot or Cronitor), and only the heartbeat URL is ever put on the host.

### 1. Uptime monitor — is the service answering?

| Setting | Value |
|---|---|
| Check | **HTTPS GET** `https://YOUR_DOMAIN/api/v1/health/ready` |
| Success | HTTP **200** (optionally also: body contains `"ready"`) — anything else, including a redirect, timeout or TLS error, is a failure |
| Timeout | 10 seconds |
| Frequency | every 1 minute (5 minutes if the plan does not allow 1) |
| Alert when | **2–3 consecutive failures** — a deploy restarts the API for a few seconds (`docs/DEPLOYMENT.md`), and one missed check should not page anyone |
| Also enable | TLS certificate expiry warning (≥ 14 days), if the service offers it |
| Request | no auth header, no body — the endpoint is public and read-only |

Why `/health/ready` and not `/health`: `/ready` runs `SELECT 1`, so a 200
proves the whole path a customer uses — DNS → TLS → Caddy → API → PostgreSQL.
`/health` only proves the Python process is alive. What it does **not**
prove: that Vapi can reach the voice endpoints, or that OpenAI is answering.
Those show up in logs and in the go-live smoke test (`docs/PILOT_LAUNCH.md`),
not in any health endpoint.

### 2. Backup heartbeat — did last night's backup reach B2?

A **dead-man's switch**: a monitor that alerts when it *stops* hearing from
something. The backup service pings it; silence is the alarm.

**How ERRS uses it** (already implemented in `docker/backup/errs_offsite.py`):

- Set `BACKUP_HEARTBEAT_URL` in the root `.env` on the host to the check's
  ping URL, then `essr up -d backup`.
- After each backup cycle **succeeds** — a new dump was taken, uploaded to
  B2, read back, and its sha256 and Object Lock verified — the service sends
  one `GET` to that URL (15 s timeout) and logs `backup_heartbeat_sent`.
- A cycle that fails anywhere (dump, upload, verification) sends **nothing**.
  The same is true if the container is stopped, crash-looping, or the whole
  VM is gone — which is precisely what the switch detects.
- If the monitoring service itself is unreachable, the backup still counts;
  `backup_heartbeat_failed` is logged and the next success pings again.
- The URL is treated as a secret (anyone holding it can mark your backups
  healthy): it is redacted from every log line and never written to the
  status files. Keep it only in the host's `.env`.

**Monitor settings:**

| Setting | Value |
|---|---|
| Type | heartbeat / cron / dead-man's-switch check, accepting a plain `GET` |
| Expected period | **1 day** (= `BACKUP_INTERVAL_SECONDS`, default 86400) |
| Grace | **4 hours** — covers a slow dump plus the hourly retries after a failed upload (`BACKUP_RETRY_SECONDS`), and matches the container going `unhealthy` at interval + 3 h |
| Alert when | no ping within period + grace |

If you change `BACKUP_INTERVAL_SECONDS`, change the monitor's period to match.
The schedule is relative to container start, so a restart simply sends the
next ping early — harmless.

### Verifying both

```bash
# Uptime: from any machine that is NOT the VM
curl -sS -o /dev/null -w '%{http_code}\n' https://YOUR_DOMAIN/api/v1/health/ready   # 200

# Heartbeat: force a cycle and confirm it pinged (exit 0 = verified off-site)
essr exec backup errs-offsite run-once
essr logs --since 10m backup | grep -E 'backup_cycle_succeeded|backup_heartbeat_(sent|failed)'
```

Then check the monitoring service shows the ping. Finally **prove the alerts
reach a human** once: pause the uptime check's target (e.g. `essr stop caddy`
for 3–4 minutes during a quiet hour, then `essr start caddy`) and confirm the
page arrived. For the heartbeat, temporarily set a short period on the check
and let it lapse.

---

## Human transfer (the caller's human exit)

Every caller can get to a person. The assistant calls its `transfer_to_human`
tool when the caller asks for a person (in any words — before intake, during
an emergency, at any point), is clearly frustrated, or needs something it
can't handle (billing, pricing disputes, complaints, judgement calls, or it has
failed to finish their request twice — for these it *offers* first).

**Where the call goes** (Owner → Settings → Human transfer; per tenant):

| Business is… | Destination | If that number is missing |
|---|---|---|
| Open (weekly hours / dated exceptions, tenant timezone) | Office number | On-call number |
| Hours not configured | Office number | On-call number |
| Closed | On-call number | **Nobody** — the closed office is never rung |

A destination that equals the tenant's AI voice-line number is refused, at save
time and again at call time (a loop straight back into the assistant).

**How it works:** ERRS POSTs a Vapi Live Call Control `transfer` command to the
call's `controlUrl`. Only a **2xx** from Vapi counts as *initiated*. The
handoff sentence ("I'm connecting you with someone at the office now." / "…with
our on-call team now.") is spoken **by Vapi as part of the accepted transfer**,
so the caller never hears "connecting you" for a transfer that was refused.
Nothing in ERRS — or Vapi — can confirm that a person answered, so nothing ever
claims it (Vapi documents that even `assistant-forwarded-call` "does not
confirm that the downstream telephony provider completed it").

**States** (`call_transfer_attempts`): `requested` → `destination_resolved` →
`initiated` | `failed`, or `requested` → `unavailable`.

| Outcome | error_code | What the caller hears |
|---|---|---|
| Initiated | — | Vapi's handoff sentence; the call is moved. No `endCall` is sent. |
| Not configured / disabled | `TRANSFER_NOT_CONFIGURED`, `TRANSFER_DISABLED` | "I can't connect you to a person right now" + offer to take details for a callback |
| After hours, no on-call number | `NO_DESTINATION_NOW` | same |
| Number is the AI line | `DESTINATION_IS_AI_LINE` | same |
| No `controlUrl` (dashboard/text channel, or `monitorPlan.controlEnabled` off) | `CALL_CONTROL_UNAVAILABLE` | same |
| Vapi refused / timed out (5 s) / network error, or caller already hung up | `PROVIDER_REJECTED`, `PROVIDER_TIMEOUT`, `PROVIDER_ERROR` | same |
| Emergency-policy transfer before the ticket exists | `EMERGENCY_TICKET_REQUIRED` | none — the assistant records the emergency first, then transfers |

**Emergencies:** the emergency ticket and its alert outbox are unchanged and
come first. A transfer never replaces, suppresses or rolls back a ticket (each
tool runs in its own savepoint). With "Connect emergency callers to a person"
on, the assistant transfers *after* the ticket is recorded. A caller who asks
for a person during an emergency is transferred regardless.

**Persistence:** the attempt row is written before Vapi is called and updated
after, inside the turn; the hang-up drain commits it even when Vapi drops the
stream as the call moves. A second transfer request in the same call does not
re-dial.

```bash
# what happened on recent transfers (numbers are masked in logs)
essr logs --since 24h api | grep call_transfer_attempted
essr exec postgres psql -U errs -d errs -c "
  SELECT status, error_code, reason, destination_kind, created_at
    FROM call_transfer_attempts ORDER BY created_at DESC LIMIT 20;"
```

**Fire-drill (before go-live, then after any Vapi change):** call the pilot
number and say "Can I speak to a person?" — once in business hours (the office
phone should ring) and once after hours (the on-call phone). Then switch human
transfer off in Settings and repeat: the assistant must say it can't connect
you and offer a callback, and must not say "connecting you".

**Limitations:** no warm transfer or whisper to the answering person (blind
transfer only); no confirmation that anyone answered; if the destination does
not pick up, what happens next is the carrier's/destination's voicemail — ERRS
is no longer on the call. Transfer destinations are PSTN numbers only (no SIP).

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

### What runs, and what it guarantees

The `backup` service (`docker/backup/`) runs one cycle immediately on start,
then every `BACKUP_INTERVAL_SECONDS` (default **nightly**). Each cycle:

1. `pg_dump -Fc` (compressed custom format) from one consistent snapshot —
   **no downtime**, the API keeps reading and writing throughout.
2. **Validates** the archive by reading all of it with `pg_restore`, and
   records its sha256 beside it (`<dump>.meta.json`).
3. **Uploads** it to Backblaze B2 with Content-MD5 (the server rejects a
   body damaged in transit), SSE-B2 encryption and an **Object Lock**
   retention of `BACKUP_OBJECT_LOCK_DAYS` (default 30, GOVERNANCE mode).
4. **Verifies** remotely: HEAD confirms size, sha256, encryption and lock;
   then the whole object is downloaded again and its sha256 recomputed.
   Only now is `<dump>.offsite.json` written and the cycle a success.
5. **Prunes local** dumps older than `BACKUP_RETENTION_DAYS` (default 7) —
   only those whose off-site copy is verified.

| | |
|---|---|
| Frequency | Nightly, relative to container start. After a failure, the pending upload is retried every `BACKUP_RETRY_SECONDS` (1h) without taking extra dumps. |
| Remote object | `B2_BUCKET/errs/postgres/<db>/<yyyy>/<mm>/errs-<yyyymmddThhmmssZ>.dump` — time-sortable, nothing from credentials or tenant data |
| Remote retention | Locked 30 days (cannot be deleted or overwritten by anyone, including this key). Hidden by the lifecycle rule on day 31, deleted on day 32. About 30 daily restore points. |
| Local retention | 7 days on the `postgres_backups` volume, for fast restores; never deleted before its off-site copy is verified |
| Deletion by the app | **Never.** The uploader issues no delete calls (tested); expiry is Object Lock + lifecycle only |
| Concurrency | One cycle at a time (`flock`); a second run exits `75` and touches nothing |
| Failure signal | Container health (`essr ps` shows `unhealthy` once the newest verified off-site copy is older than interval + 3h), structured logs, and the `BACKUP_HEARTBEAT_URL` dead-man's switch — the only one that still alerts when the whole VM is gone (see **External monitoring**) |

### One-time Backblaze B2 setup

**Bucket** (`ERRS-production-backups`): Private · Default encryption SSE-B2 ·
Object Lock enabled. A default retention on the bucket is optional — every
upload sets its own.

**Lifecycle rule** (B2 web UI → Buckets → Lifecycle Settings → custom rules).
This is what expires backups; the application never does:

```json
[
  {"fileNamePrefix": "errs/postgres/", "daysFromUploadingToHiding": 31,
   "daysFromHidingToDeleting": 1, "daysFromStartingToCancelingUnfinishedLargeFiles": 1},
  {"fileNamePrefix": "restore-drill/", "daysFromUploadingToHiding": 2,
   "daysFromHidingToDeleting": 1, "daysFromStartingToCancelingUnfinishedLargeFiles": 1}
]
```

Hiding (day 31) comes after the 30-day lock has expired, so B2 never has to
refuse the lifecycle's own deletion. If you raise `BACKUP_OBJECT_LOCK_DAYS`,
raise `daysFromUploadingToHiding` with it.

**Application key** — dedicated, restricted to this bucket, with only what the
uploader uses. The web UI's "Read and Write" preset also grants `deleteFiles`
and `shareFiles`, which a backup key should not have; create it with the
[B2 CLI](https://www.backblaze.com/docs/cloud-storage-command-line-tools)
instead, from a machine where you are logged in with your master key:

```bash
b2 key create --bucket ERRS-production-backups errs-backup-uploader \
  listBuckets,listFiles,readFiles,writeFiles,readFileRetentions,writeFileRetentions,readBucketEncryption,readBucketRetentions,readBucketLifecycleRules
```

| Capability | Why |
|---|---|
| `writeFiles` | upload |
| `readFiles`, `listFiles` | read-back verification, `list`, restore |
| `writeFileRetentions`, `readFileRetentions` | set and verify each upload's Object Lock |
| `listBuckets` | S3 API bucket resolution, `check` |
| `readBucketEncryption`, `readBucketRetentions`, `readBucketLifecycleRules` | optional — lets `check` confirm SSE, Object Lock and the lifecycle rule on the bucket |
| **not** `deleteFiles`, `bypassGovernance`, `writeBucketRetentions`, `writeBuckets`, `shareFiles`, `writeBucketLifecycleRules`, `writeBucketReplications`, `writeBucketEncryption`, `writeBucketNotifications` | a stolen backup key must not be able to destroy or publish backups |

Put the key ID and key in the root `.env` on the deployment host
(`B2_APPLICATION_KEY_ID`, `B2_APPLICATION_KEY`, `B2_BUCKET`; see
`docker/compose.env.production.example`). The production stack refuses to
start without them. Then:

```bash
essr up -d --build backup
essr exec backup errs-offsite check      # every line must be PASS
```

`check` prints PASS/FAIL for: key authorizes, key restricted to this bucket,
required capabilities present, destructive capabilities absent, bucket
private, SSE-B2 default, Object Lock enabled, and an **unauthenticated** GET
of a real backup being refused. It never prints a credential.

### Everyday

```bash
# the latest successful backup (from the service's own record)
essr exec backup errs-offsite status

# is it healthy? (unhealthy = no verified off-site copy for > interval + 3h)
essr ps backup
essr exec backup errs-offsite health

# what is actually in B2, newest first — the authority during an incident
essr exec backup errs-offsite list

# local dumps on this host
essr exec backup sh /usr/local/bin/errs-restore.sh --list

# take one right now (before a risky change); exit 0 only once it is
# verified off-site, 75 if a scheduled cycle is already running
essr exec backup errs-offsite run-once

# history
essr logs --since 48h backup | grep -E 'backup_cycle_(succeeded|failed)|offsite_upload_(verified|failed)'
```

### Failure behaviour

| Situation | What happens |
|---|---|
| `pg_dump` fails | `backup_failed`; the `.partial` is removed; cycle fails; retried in 1h |
| B2 unreachable / 5xx / timeouts | retried with backoff (4 attempts); then `offsite_upload_failed`, cycle fails, dump kept locally and re-uploaded by every later cycle until it succeeds |
| Credentials rejected (401/403) | not retried (it cannot succeed); `fatal: true` in the log, cycle fails; the key never appears in output |
| Upload cut mid-body | S3 PUT is atomic, so nothing is stored; retried; verified afterwards |
| Local dump changed since it was taken | refused (`refusing to upload a changed or corrupted backup`), kept for inspection, never marked uploaded |
| Remote key exists with other content | refused, never overwritten |
| Remote object corrupt at restore time | `download` refuses (sha256 mismatch) and deletes its partial file; the restore script stops before touching a database |
| Truncated/broken archive | rejected at dump time; at restore time `pg_restore --exit-on-error --single-transaction` fails and leaves the target empty |
| Two cycles at once | second exits `75`, no second dump, no duplicate upload |
| Killed mid-dump or mid-upload (restart, deploy, crash) | `.partial` ignored; unverified dump has no marker, so the next cycle uploads it; the lock dies with the process |
| Disk filling with un-uploaded dumps | logged every cycle as `local_prune_blocked_not_offsite` — fix B2 access; never "solved" by deleting the only copy |

Each row is a test in `docker/backup/tests/` (`scripts/backup-tests.sh`).

### Restore a backup

The order matters. Restore into a **copy**, verify the copy, and only then
swap. A restore straight over the live database destroys the very data you
would need if the backup turned out to be bad.

```bash
# 1. RESTORE into a new database (live is untouched; the script refuses
#    --target errs without an explicit --force-live). Either the newest
#    local dump, or the newest OFF-SITE one:
essr exec backup sh /usr/local/bin/errs-restore.sh --latest --target errs_restored
essr exec backup sh /usr/local/bin/errs-restore.sh --from-offsite latest --target errs_restored

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

### Restore after losing the whole VM

Nothing from the old host is needed except what is in B2 and your secrets
(the root `.env` and `apps/api/.env` — keep a copy in your password manager,
not in B2 next to the data they protect).

```bash
# on the new host, per docs/DEPLOYMENT.md: clone, restore both .env files, then
essr up -d --build                      # empty database, migrated to head
essr exec backup errs-offsite list      # pick the restore point
essr exec backup sh /usr/local/bin/errs-restore.sh --from-offsite latest --target errs_restored
# (or --from-offsite <key> for an older point)
# then VERIFY and SWAP exactly as above
```

`--from-offsite` downloads into the backups volume, refuses anything whose
sha256 differs from the value recorded at backup time, re-validates the
archive, and only then restores. The new host's own first backup cycle runs
on the (empty) database at startup. That is harmless — it cannot overwrite or
delete any existing off-site backup — and `--from-offsite latest` skips
backups that recorded zero organizations (it logs
`offsite_latest_skipped_empty`), so "latest" still means the newest backup
with real data. Check the key it prints against `list` before you swap.

### Restore drill (monthly, and after any change to backups)

```bash
scripts/backup-restore-drill.sh
```

Dumps the running database (read-only), uploads it to B2 under
`restore-drill/<run>/` with a 1-day lock, then on a **brand-new, isolated**
PostgreSQL container: downloads it, restores it with the production script,
and compares **every table's row count and full-row digest**, every foreign
key (all validated), index, enum and sequence against the source; checks the
Alembic revision and `alembic check`; starts the API against it
(`/health/ready`) and loads every ORM model. Everything it creates is removed
on exit. It never writes to the source and never restores over anything.
`--local-s3` runs the same drill against a throwaway S3 server when B2 is not
configured (a pipeline check, not proof of B2).

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

- Requests reaching Caddy but rejected by the API (`401`) → the `x-vapi-secret`
  Vapi sent does not match `VAPI_SERVER_SECRET`. Check **which endpoint** 401s:
  `/chat/completions` reads `assistant.model.headers["x-vapi-secret"]`,
  `/events` reads `assistant.server.headers["x-vapi-secret"]`. A 401 on
  `/chat/completions` (Vapi: `custom-llm-401-unauthorized`) while `/events`
  succeeds means only the Server URL secret was set — the dashboard's webhook
  screen never updates the model header.
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
| `JWT_SECRET_KEY` | Edit `apps/api/.env`, `essr up -d api` (recreates the container; a plain `restart` keeps the old value) | Every **access** token (15 min) becomes invalid, including any forged with a leaked secret. Refresh tokens are opaque and stored hashed, so they are unaffected: signed-in users silently get new access tokens on their next refresh. |
| `VAPI_SERVER_SECRET` | **Three places, same value:** update `assistant.model.headers["x-vapi-secret"]` (Custom LLM) **and** `assistant.server.headers["x-vapi-secret"]` (Server URL) on the Vapi assistant, then `apps/api/.env`, then `essr up -d api` | Calls fail while any of the three disagree — keep the gap short. Updating only the Server URL secret breaks every call (Custom LLM 401) |
| `POSTGRES_PASSWORD` | `ALTER USER errs WITH PASSWORD '…';` then update `.env` **and** `apps/api/.env`, then `essr up -d` | API and backup service both need the new value |
| `B2_APPLICATION_KEY` | Create the new key (same capabilities, see Backups), update `.env`, `essr up -d backup`, `essr exec backup errs-offsite check`, **then** delete the old key in B2 | None if done in that order; existing backups stay locked and readable |
| `OPENAI_API_KEY` | Edit `apps/api/.env`, `essr up -d api` | Calls in flight fail; new ones use the new key |
| A tenant's webhook URL | Dashboard → Settings → Emergency alerts → paste the new one (replaces) | That tenant's alerts only |

Never commit any of these. `.gitignore` excludes `.env` files; `apps/api/.env.example`
and `docker/compose.env.production.example` are the templates.

---

## What this deployment does *not* yet do

Stated so nobody discovers it during an incident:

- **Backups are daily, not continuous.** A restore loses up to a day of
  writes (no WAL archiving / point-in-time recovery).
- **Alerting is external and only as good as its setup.** API-down and
  missed-backup alerts exist only once the two monitors in **External
  monitoring** are configured. Nothing pages you when a migration fails or
  when emergency notifications start failing for one tenant; checking the
  `emergency_notification_attempted` query above daily is the minimum viable
  substitute during a pilot.
- **No zero-downtime deploys.** A deploy drops in-flight calls for a few
  seconds.
- **Rate limits are per worker**, so the effective ceiling is roughly 4× the
  configured value.
- **No data-deletion / retention tooling.** Transcripts and customer records
  accumulate indefinitely; there is no self-serve erasure path yet.
- **Voice line provisioning is manual** — a SQL insert, no UI.

These are Phase 3 items, not oversights.
