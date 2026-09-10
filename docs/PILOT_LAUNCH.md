# ESSR pilot launch: telephony wiring and tenant onboarding

What has to happen *after* the stack is deployed and before a real caller
hears anything useful.

`docs/DEPLOYMENT.md` gets the infrastructure up. `docs/RUNBOOK.md` keeps it
running. This file covers the two things in between: pointing Vapi at the
deployment, and configuring the first business.

Throughout:

```bash
alias essr='docker compose -f docker-compose.yml -f docker-compose.prod.yml'
```

---

## Part 1 — Telephony wiring

Three distinct places hold configuration. Confusing them is the most common
reason a pilot's first call fails, so they are listed separately.

### A. Repository side (already done)

Nothing to change in the code. For reference, these are the endpoints Vapi
will call, both mounted under the `/voice/vapi` router and both gated by the
same shared-secret dependency:

| Purpose | URL |
|---|---|
| Custom LLM (every conversational turn) | `https://YOUR_DOMAIN/api/v1/voice/vapi/chat/completions` |
| Server URL (lifecycle events) | `https://YOUR_DOMAIN/api/v1/voice/vapi/events` |
| Auth header on both | `x-vapi-secret: <VAPI_SERVER_SECRET>` |

The secret is compared in constant time and **fails closed**: an unset
`VAPI_SERVER_SECRET` rejects every request rather than accepting all of them.
Production refuses to boot without it, so this cannot be silently missed.

Note that the secret only proves *the request came from our Vapi account*. It
never decides which organization the call belongs to — that is resolved
separately from `voice_lines` by assistant id. Two tenants sharing one Vapi
account still cannot see each other's data.

### B. Vapi dashboard (manual — you must do this)

On the assistant that will serve the pilot business:

| Setting | Value | Why |
|---|---|---|
| Model → Custom LLM URL | `https://YOUR_DOMAIN/api/v1/voice/vapi/chat/completions` | Every turn goes here |
| Model → streaming | **enabled** | The API streams SSE; Caddy is configured with `flush_interval -1` so tokens are not buffered. Without streaming the caller waits for the whole turn in silence. |
| Server URL | `https://YOUR_DOMAIN/api/v1/voice/vapi/events` | Lifecycle events (status, transcript, end-of-call) |
| Server URL Secret | the same value as `VAPI_SERVER_SECRET` | Sent as `x-vapi-secret` |
| First message | the business's greeting | Spoken before the first model turn; ESSR does not supply it |
| End call function | **enabled** | The API emits an `endCall` tool call when a turn should hang up (completion gate, or a disabled assistant) |

**Replace any ngrok/tunnel URL.** The repository contains no tunnel URLs —
they only ever existed in the Vapi dashboard, and both fields above must now
point at the stable domain.

**Leave unchanged:** transcriber settings, voice selection, and the
LiveKit smart-endpointing configuration. That endpointing config was tuned
deliberately and lives only in the dashboard; re-tuning it is not part of
going live.

### C. Twilio (manual — you must do this)

Twilio's only job here is to own the phone number and hand calls to Vapi.

1. Import the number into Vapi (Vapi dashboard → Phone Numbers → import from
   Twilio), **or** point the number's Voice webhook at Vapi if you are
   routing manually.
2. Attach the number to the assistant configured in section B.

**ESSR's application code never calls Twilio.** `TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN` and `TWILIO_PHONE_NUMBER` exist in `Settings` but are read
by nothing — verified by grep across `apps/api/app`. Leave them blank unless
and until something actually uses them. The same is true of `VAPI_API_KEY`:
the backend never calls Vapi's API, only receives its webhooks.

---

## Part 2 — Onboarding the first business

Every step below has a real API **except one**, which needs a SQL insert.

Sign in as the organization's Owner for all API steps; each request is scoped
to the caller's own organization by their JWT.

### 1. Create the organization and Owner — API

```
POST /api/v1/auth/register
  { organization_name, full_name, email, password }
```

This creates the organization, the Owner user, and seeds the four default
roles (Owner / Admin / Member / Technician) with their permissions.

> **Close self-service signup once the pilot tenants exist.** Set
> `FEATURE_REGISTRATION_ENABLED=false` in `apps/api/.env` and restart the
> API; `/auth/register` then answers `403 REGISTRATION_DISABLED`. Existing
> users still log in, and new teammates are still added from the Team page,
> so this shuts the front door without locking anyone out.
>
> Register the pilot organizations *before* flipping it — with registration
> closed there is no other way to create one.

### 2. Business knowledge — API

This is what the assistant is allowed to know. All of it feeds the system
prompt; none of it is hardcoded anywhere.

| What | Endpoint |
|---|---|
| Profile (name, type, timezone, address) | `PUT /api/v1/business-knowledge/profile` |
| Weekly hours (all 7 days) | `PUT /api/v1/business-knowledge/hours` |
| Holiday / exception days | `POST /api/v1/business-knowledge/hours/exceptions` |
| Services (name, duration, emergency-eligible) | `POST /api/v1/business-knowledge/services` |
| Service areas | `POST /api/v1/business-knowledge/service-areas` |
| FAQs | `POST /api/v1/business-knowledge/faqs` |
| Emergency keywords | `POST /api/v1/business-knowledge/emergency-keywords` |

**Timezone and weekly hours are load-bearing.** The availability engine
derives bookable slots from them; a business with no hours configured has no
availability, and the assistant will correctly but uselessly tell every caller
it cannot find a time.

**Service `default_duration_minutes` decides visit length.** With no matching
service the system falls back to `SCHEDULING_DEFAULT_DURATION_MINUTES` (60).

### 3. Emergency alert destination — API or UI

**Dashboard → Settings → Emergency alerts**, or:

```
PUT /api/v1/organizations/current/notifications
  { "channel": "webhook", "destination": "https://...", "is_enabled": true }
```

Requires `NOTIFICATION_PROVIDER=webhook` in `apps/api/.env`. The destination
must be a public HTTPS URL — loopback, private, link-local and reserved
addresses are rejected as an SSRF control, and plain http is rejected because
the payload carries the caller's name, number and address.

**Until this is set, emergency callers are told their request is logged but
that the alert could not be confirmed.** That is correct behaviour, not a
bug — but it is not what a pilot wants, so treat it as required onboarding.

### 4. Technicians — API (optional but recommended)

```
POST /api/v1/team/members                      # create the user
POST /api/v1/dispatch/technicians              # make them a technician
PATCH /api/v1/dispatch/technicians/{id}/on-call
```

Capacity matters: with **no** technician profiles the availability engine
uses `SCHEDULING_DEFAULT_CAPACITY` (1 concurrent appointment). Once a roster
exists, the count of on-call technicians is used instead.

### 5. Voice line — ⚠️ MANUAL SQL, no API exists

This row is what maps an inbound Vapi assistant id to an organization.
Without it every call fails with `voice_line_not_found`.

```bash
essr exec postgres psql -U errs -d errs -c "
  INSERT INTO voice_lines
    (id, organization_id, provider, vapi_assistant_id,
     vapi_phone_number_id, phone_number, is_active, created_at, updated_at)
  VALUES
    (gen_random_uuid(),
     (SELECT id FROM organizations WHERE name = 'THE BUSINESS'),
     'VAPI',
     'asst_xxxxxxxxxxxx',
     NULL,
     '+15551234567',
     true, now(), now());"
```

> **`provider` must be the uppercase string `'VAPI'`.**
> The Python enum is `VoiceProvider.VAPI = "vapi"`, but the column is a
> `native_enum=False` SQLAlchemy Enum, which persists the member **name**, not
> its value. Inserting `'vapi'` produces a row the ORM cannot map back, so the
> line silently fails to resolve and every call is rejected. This project has
> been caught by this enum-casing behaviour more than once; it is the single
> most error-prone step in onboarding.

Verify immediately:

```bash
essr exec postgres psql -U errs -d errs -c "
  SELECT o.name, v.vapi_assistant_id, v.provider, v.is_active
    FROM voice_lines v JOIN organizations o ON o.id = v.organization_id;"
```

Then confirm the API agrees (this is the read path a call actually uses):

```
GET /api/v1/voice/line      # as that org's Owner — must return the row
```

### 6. Confirm the kill switch is on

```
GET /api/v1/organizations/current    # voice_assistant_enabled must be true
```

New organizations default to enabled, so this is a check rather than a step.

---

## Before you go live

Decisions and known gaps that should be settled deliberately rather than
discovered on a real call.

| Item | Status | Note |
|---|---|---|
| Open registration | **Closeable** | `FEATURE_REGISTRATION_ENABLED=false` refuses signup with `403 REGISTRATION_DISABLED`, before the submitted address is looked up — so a closed deployment cannot be used to test whether an account exists. Left `true`, anyone who finds the domain can create an organization. Decide deliberately. |
| Registration enumeration | Present | A duplicate email returns a distinguishable error, so account existence is discoverable. |
| API docs | Not reachable in production | `/docs`, `/redoc` and `/openapi.json` are mounted at the **root**, not under `/api/v1`, so Caddy routes them to the frontend and they 404. Fine — arguably desirable on a public deployment — but it is accidental rather than chosen. |
| Backups | Same host only | Survive a container rebuild, not the loss of the VM. |
| Alerting | None | Nothing pages you if the API dies or emergency alerts start failing. The `RUNBOOK.md` queries are the manual substitute. |
| Rate limits | Per worker | 4 uvicorn workers each count separately, so the effective ceiling is roughly 4× the configured value. |
| `secure` cookie | Requires HTTPS | The refresh cookie sets `secure=True` in production. Logging in over plain http will appear to succeed and then immediately bounce back to `/login`, because the browser silently drops the cookie. Always test over the real domain. |

---

## Go-live smoke test

Run in order. Stop at the first failure.

**Infrastructure**

```bash
essr ps                                            # all Up; api/frontend healthy
curl -sS https://YOUR_DOMAIN/api/v1/health         # {"status":"ok"}
curl -sS https://YOUR_DOMAIN/api/v1/health/ready   # {"status":"ready"}
curl -sSI https://YOUR_DOMAIN/ | head -1           # 200, real certificate
nc -zv YOUR_DOMAIN 5432                            # MUST fail
essr exec api alembic current                      # expected revision
```

**Application** — in a browser, over the real domain:

1. `/login` renders; sign in as the Owner; you land on the dashboard.
2. Business Knowledge shows what you configured in step 2.
3. Settings → Emergency alerts shows a masked destination (never the URL).
4. Settings → Voice assistant shows "Answering".

**Webhook authentication** — the negative test matters most:

```bash
# no secret → must be rejected
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  https://YOUR_DOMAIN/api/v1/voice/vapi/chat/completions \
  -H 'content-type: application/json' -d '{}'          # expect 401
```

**A real call** — place one to the pilot number and watch:

```bash
essr logs -f api | grep -E 'voice_request_received|voice_line_resolved|voice_tool_executed|emergency_notification_attempted'
```

| Scenario | What to check |
|---|---|
| Non-emergency booking | Assistant offers times, you pick one **in a later turn**, it books. Appointment appears in the dashboard with the time you chose. |
| Consent invariant | The assistant must not book a time you never selected. Offering is not choosing. |
| Emergency | Ticket appears in Dispatch; your webhook endpoint receives the alert; the assistant says a dispatcher was alerted **only if** it did. |
| Emergency with alerting off | Assistant says the request is logged but the alert could not be confirmed — and does *not* claim a dispatcher was notified. |
| Kill switch | Disable in Settings, call again: you hear the "automated assistant is unavailable" message and the call ends. Re-enable. |
| Customer record | The caller appears under Customers, deduplicated by phone number. |

**Persistence and recovery**

```bash
essr restart api && sleep 30 && curl -sS https://YOUR_DOMAIN/api/v1/health/ready
essr exec backup sh /usr/local/bin/errs-restore.sh --list   # a dump exists
```

Data written before the restart must still be present afterwards.
