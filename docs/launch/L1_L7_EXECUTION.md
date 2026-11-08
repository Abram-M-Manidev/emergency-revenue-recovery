# L1–L7 execution plan and status — 2026-11-08

Source of truth for each definition: the "Launch blockers" table in
`LAUNCH_ROADMAP.md`. The acceptance criteria below are taken from the
documents each blocker points to (`DEPLOYMENT.md`, `REAL_CALL_ACCEPTANCE_PLAN.md`,
`PROVIDER_READINESS.md`, `docs/legal/`), not invented here. "Verified" says
how: **auto** (automated tests), **mock** (tests against fakes), **read-only**
(a GET against a real provider), **real** (a real call/SMS/transfer), or
**prod** (on the production deployment). Nothing in this document has been
verified **real** or **prod** except where stated.

## Tools added for this work (2026-11-08)

| Tool | Purpose | Used for |
|---|---|---|
| `python -m app.cli.preflight` | Read-only go-live check: migrations at head; alert/paging providers; per active line: assistant on, alert destination, country, timezone, hours, services, transfer, paging; usage health. Exit 1 on any FAIL. Prints codes and org ids only. | L1, L2, L3 preconditions |
| `python -m app.cli.call_check <vapi_call_id> [--vapi]` | Evidence for one call: classification, ticket, alert delivery status, page status and acknowledgement, transfers, follow-up, appointment, interrupted replies, corrections, usage. `--vapi` adds one read-only GET: recording present, first assistant line timing and whether it carried the AI/recording notice, first caller line timing. No transcript text or numbers printed. | L3, L4, L7 |
| `TRANSFER_NOT_CONNECTED` follow-up | When Vapi ends a call with `call.forwarding.no-answer`, `call.forwarding.operator-busy` or `call.in-progress.error-transfer-failed` after an initiated transfer, a callback follow-up is raised even if a request was recorded. | L4 |

---

## L1 — Production host + domain + TLS deployed (`docs/DEPLOYMENT.md`)

Acceptance (from `DEPLOYMENT.md` §5 "Verify before telling anyone it is live"):
1. All services up; api and frontend healthy.
2. Migrations at the expected revision.
3. `https://DOMAIN/api/v1/health` → ok; `/api/v1/health/ready` → ready.
4. Real (not self-signed) certificate.
5. `/login` renders (200).
6. Postgres not reachable from outside.
7. First backup taken, uploaded and verified off-site (`errs-offsite check`).
8. NEW: `python -m app.cli.preflight` reports 0 FAIL.

| Criterion | Status | Verified |
|---|---|---|
| Production compose, Caddy TLS, env validation, backups exist in repo | Done (earlier milestones) | auto; local prod rehearsal 2026-10-05 |
| Production API image builds and imports the app | Done 2026-11-08 | auto |
| Production frontend image builds and serves `/`, `/privacy`, `/terms` | Done 2026-11-08 | auto |
| 1–8 on a real host | **Not started** — no host, domain or AWS access | — |

Procedure: `DEPLOYMENT.md` "First deployment" steps 1–8, then run the
preflight inside the api container. Cost (ESTIMATE): t4g.small ~$15–20/mo +
domain ~$10–15/yr. Risk: 2 GiB RAM is tight for api+frontend+postgres+backup
(see EC2 sizing notes). Rollback: the host is new — stop the stack; nothing
else depends on it until Vapi is pointed at it (that is L3's step).

**Assessment: NO-GO (blocked on external resources).** Engineering is ready.

## L2 — Apply migrations to production (and dev) DB to head `3d4e5f6a7b8c`

Acceptance: dev and production databases at `3d4e5f6a7b8c`; a verified
backup taken immediately before; no existing data changed; API healthy after.

| Criterion | Status | Verified |
|---|---|---|
| Dev DB at head | **Done 2026-11-08** (backup restore-verified first, counts unchanged, lead and correction flows exercised) | real (dev DB) |
| Full chain (30 revisions) base → head → base → head, no drift | **Done 2026-11-08** on a disposable database | auto |
| Preflight `migrations_at_head` | PASS on dev | read-only (dev) |
| Production at head | **Not possible** — no production database exists yet | — |

Production procedure (after L1):
```bash
essr exec backup errs-offsite check                      # last backup is good
essr exec postgres pg_dump -U errs -d errs -Fc -f /tmp/pre_migration.dump
essr exec api alembic current                            # record the starting revision
essr exec api alembic upgrade head
essr exec api alembic current                            # expect 3d4e5f6a7b8c (head)
essr exec api python -m app.cli.preflight
```
On a fresh production database the whole chain runs from empty (verified
above). Rollback: `alembic downgrade <recorded revision>`; the two newest
revisions only add tables (`contact_correction_requests`, `marketing_leads`),
so downgrading them drops those tables and their rows (correction audit
trail, website leads) — export leads first (`app.cli.leads`). Older
downgrades are documented in each migration file; restoring the pre-migration
dump is the last resort.

**Assessment: dev GO (done); production CONDITIONAL GO — run immediately after L1.**

## L3 — Real PSTN acceptance run (`REAL_CALL_ACCEPTANCE_PLAN.md`), esp. #8–#12

Acceptance: every scenario 1–18 in the plan run on the deployed line and
recorded with pass/fail and `call_check` output; #8 (emergency), #9 (gas),
#10 (paging), #11 (transfer success), #12 (transfer failure) must pass.

| Criterion | Status | Verified |
|---|---|---|
| Plan written with scripts and pass criteria | Done | — |
| Evidence tool (`call_check`) | Done 2026-11-08 | auto; read-only against one real call |
| Preconditions on the test org (alert destination, paging, transfer numbers, country) | **Not met on dev**: preflight shows all 18 active dev lines have no alert destination | read-only (dev) |
| Scenarios 1–18 | **Not run** — needs real calls (authorization) and ideally L1 | — |

Procedure: configure a test organization (operator-owned Slack/webhook,
operator phone for paging and transfer, country US), run preflight to 0
FAIL, then each scenario followed by `call_check <id> --vapi`. Cost: a few
dollars of Vapi/Twilio/OpenAI per full run (ESTIMATE ~$0.10–0.15 per AI
minute). Risk: real tickets/alerts are created in the test org — use only
operator-owned destinations. Rollback: none needed; test records stay in the
test org.

**Assessment: NO-GO until run.** Tooling and plan are ready.

## L4 — Real Twilio SMS page + human-transfer drill

Acceptance (`PROVIDER_READINESS.md` gaps 1–2): a real SMS page reaches the
operator phone, its acknowledgement link works once, the dashboard shows
sent → acknowledged; a real transfer connects to a staffed number and the
attempt is visible; a transfer that is not answered leaves the business a
visible callback.

| Criterion | Status | Verified |
|---|---|---|
| Paging state machine (queued/sent/acknowledged, HMAC ack links, retries) | Done | mock |
| Transfer via Vapi live control; honest wording; attempts visible | Done | mock |
| Unanswered transfer → callback follow-up | **Added 2026-11-08** (`TRANSFER_NOT_CONNECTED`) | auto/mock |
| Real SMS delivered + acknowledged | **Not done** — needs funded Twilio + approval | — |
| Real transfer connected | **Not done** — needs transfer numbers + approval | — |
| Real unanswered transfer produces the follow-up | **Not done** | — |

Key fact (Vapi docs, read 2026-11-08, docs.vapi.ai/tools/transfer-call): on
Twilio numbers a blind transfer that is busy or unanswered ENDS the call;
Vapi's `fallbackPlan` (caller returned to the assistant) exists only on Vapi
numbers and BYO SIP, and no server message reports a failed transfer while
the call is live. So today a dropped caller is recovered only by the new
follow-up. Returning the caller to the assistant is L8 (Vapi configuration),
not started here.

Procedure: set `PAGING_PROVIDER=twilio` with real credentials and
`PAGING_ACK_BASE_URL`, run scenario #10; set transfer numbers to the operator
phone, run #11 answering and #12 not answering; `call_check` each. Cost:
Twilio SMS ~$0.01 each + number rental (ESTIMATE). Rollback: set
`PAGING_PROVIDER=none` / disable transfer in Settings.

**Assessment: NO-GO until the real drill passes.**

## L5 — Vapi paid plan

Acceptance: the Vapi org is on a paid plan whose concurrency covers the
pilot (free tier: 4 concurrent calls across ALL tenants, 14-day retention).

Facts (vapi.ai/pricing, read 2026-11-08 — re-check before buying): Core
$29/month, 10 concurrent calls, 30-day raw data retention; Pro 10% of
hosting with a $999/month minimum, 30 concurrent; extra concurrency
$10/line/month. Hosting $0.05/min on top in all cases.

Recommendation: **Core** for a 1–3 customer pilot. Note the retention change
(14 → 30 days) in `docs/legal/DATA_RETENTION_POLICY.md` and the privacy
page when bought. Risk: none to the code. Rollback: downgrade in the Vapi
dashboard.

**Assessment: NO-GO until purchased (paid action — yours).**

## L6 — Legal review

Acceptance: entity decided; privacy policy, terms, DPA and pilot agreement
reviewed by a lawyer; "Draft" banners removed only after that.

Status: drafts complete in `docs/legal/` (the pilot agreement is kept outside
this repository); questions for counsel in `LEGAL_REVIEW_CHECKLIST.md`. Not
reviewed. No engineering remains except removing the draft banner from
`/privacy` and `/terms` once approved text exists.

**Assessment: NO-GO until a lawyer signs off.**

## L7 — Recording-before-notice verified on a real call; decide mitigation

Acceptance: on a real call, establish when recording starts relative to the
spoken AI/recording notice, and record a decision on any gap.

Evidence (read-only, 2026-11-08, `call_check <id> --vapi` on the
2026-10-31 dev test call, assistant in model-generated first-message mode):
- recording present (Vapi records from the start of the call; its docs do not
  state when recording begins relative to answer — "not found");
- first assistant line at **2.02 s** and it contained both the AI notice and
  the recording notice;
- first caller speech at **14.69 s** — the notice preceded anything the
  caller said.

So the recording contains about 2 seconds before the notice, during which
the caller had not spoken. Options for counsel/founder:
1. Accept (no caller speech is captured before the notice).
2. Speak the notice as a static `firstMessage` (Vapi plays it immediately on
   answer, likely under 2 s; the ERRS opening path then returns a short
   greeting) — Vapi configuration change, needs approval.
3. Disable Vapi recording (`artifactPlan.recordingEnabled=false`) and keep
   ERRS transcripts only.

Remaining: repeat the measurement on the production line during L3 (#1) —
one call — because the measurement above is from the dev line.

**Assessment: CONDITIONAL GO** — evidence gathered and the notice comes
first; needs one confirmation on the production line and a recorded
decision from counsel on the ~2 s pre-notice window.

---

## Outstanding approvals and external actions (single list)

| # | Action | Needs | Cost (est.) | Rollback |
|---|---|---|---|---|
| A1 | Provision host, domain, DNS, TLS (L1) | AWS account access, domain purchase | ~$15–20/mo + domain | Stop stack |
| A2 | Production migration to head (L2) | Your approval after A1 | — | `alembic downgrade` / restore dump |
| A3 | Buy Vapi Core (L5) | Billing access | $29/mo + $0.05/min | Downgrade |
| A4 | Configure test org alerts/paging/transfer with operator-owned destinations (L3 precondition) | Your Slack/webhook + phone | — | Delete settings |
| A5 | Point Vapi assistant at production URL + secret (L1/L3) | Vapi dashboard approval | — | Re-point to old URL |
| A6 | Real PSTN acceptance calls 1–18 (L3, L7) | Approval to call | a few $ | — |
| A7 | Real SMS page + transfer drills (L4) | Funded Twilio, transfer numbers, approval | cents | Disable paging/transfer |
| A8 | Lawyer review (L6) | Engagement | legal fees | — |
| A9 | Decide L7 mitigation (accept / static firstMessage / no recording) | Counsel + you | — | Vapi PATCH back |
