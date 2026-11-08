# Provider integration readiness — 2026-11-08

"Mocked" means covered by automated tests with fakes or local servers only.
"Live" means exercised against the real provider. Nothing below was
exercised live in this session; dates of earlier live checks are given.

| Provider | Auth | Timeouts / retries | Failure behaviour (no false success) | Secrets in logs | Verified how |
|---|---|---|---|---|---|
| **Vapi → ERRS webhook** (`/voice/vapi/chat/completions`, `/events`) | `x-vapi-secret`, constant-time compare, fails closed if unset; Custom-LLM secret lives in `assistant.model.headers` | Turn bounded by `OPENAI_REALTIME_TIMEOUT_SECONDS=15`, 0 retries; tool calls 10 s | Provider outage → fixed outage reply, ticket if hazard/keyword words, transfer attempt, follow-up record (B2). Interrupted stream → reply marked unheard (P1). Duplicate/superseded requests → cached reply under advisory lock | Header never logged | Mocked (1350 tests); live dev calls 2026-08-22 … 2026-10-31 |
| **Vapi API** (reconciliation `GET /call`) | Private `VAPI_API_KEY` | 4 attempts, pacing 0.35 s (≤172/min vs 200/min org limit) | Run is all-or-nothing, recorded FAILED with error type; `usage_health` alerts | Key never logged | Live read-only dry runs 2026-10-30 (0 calls found — parser unproven on a real payload) |
| **Vapi transfer** (`controlUrl`) | Per-call URL from Vapi | Bounded request | Transfer success only when Vapi accepts; failure → spoken honestly, callback offered; attempts visible in dashboard (B8) | Numbers masked in API | Mocked; `controlEnabled` enabled 2026-09-30; **no real PSTN transfer drill** |
| **OpenAI** | `OPENAI_API_KEY` (required in production) | Realtime 15 s/0 retries; quality 45 s/1 retry; ≤5 tool rounds | `AIProviderUnavailableError` → outage path; strict JSON schema, invalid output never persisted | Key never logged | Mocked; live dev calls with gpt-4.1-mini |
| **Deepgram** | Inside Vapi | Vapi-managed | ERRS sees only transcripts; mis-hearing mitigated by phone validation and read-back rules | n/a | Live via Vapi only |
| **Twilio (PSTN number)** | Vapi-attached number | Carrier | n/a | n/a | Live dev calls |
| **Twilio (SMS paging)** | `TWILIO_ACCOUNT_SID/AUTH_TOKEN` | 10 s, 3 attempts, 20 s base backoff; lease/fence outbox | Page state only "sent" after provider 2xx; "acknowledged" only via HMAC ack link; never "on the way" | Token never logged; numbers masked | **Mocked only — never sent a real SMS** |
| **Webhook alerts** (Slack/generic) | URL per tenant | 5 s, 8 attempts, backoff, outbox after commit | Delivered ≠ human alerted (B4): caller hears "sent to the team" only | URL masked | Mocked + local server tests |
| **PostgreSQL** | Compose credentials | Pool 10; savepoints isolate best-effort writes | A failed best-effort write never rolls back the turn | DSN not logged | Live (dev/test DBs) |
| **Backblaze B2** | App key (scoped) | restic retries | Heartbeat monitor alerts on missed backup | Not logged | Live drill PASSED 2026-09-28 |

## Gaps that must be closed before a paid pilot
1. A real SMS page through Twilio to an operator phone (needs funded Twilio
   number + approval to send).
2. A real human-transfer drill to a staffed number (needs transfer numbers).
3. One real reconciliation run that parses an actual Vapi Call payload
   (needs one real call in the 14-day window, then `usage_reconcile`).
4. Confirm Vapi recording start vs. the spoken notice on a real call.
5. Decide behaviour when an accepted transfer is not answered (Vapi
   transfer fallback setting) — B5, Vapi configuration, not code.
