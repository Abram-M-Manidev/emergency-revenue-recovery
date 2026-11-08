# ESSR consolidated launch roadmap — 2026-11-08 → 2026-12-11

## 1. Where the project stands

**Product roadmap (ROADMAP.md):** milestones 1–10 (Foundation … Production
Polish) are complete and committed.

**Hardening phases since then (all committed):** P1 interrupted replies,
Phase A–D (model migration, human transfer, emergency paging, disclosure +
life safety), acceptance fixes B1–B4/B8, metering M1–M2, conversation-audit
Phase 1 (availability widening) and Phase 2 (phone validation, privacy,
corrections), and the 2026-11-08 launch work:
- Address corrections committed only after a confirmed, heard read-back
  (backend-enforced, `contact_correction_requests`).
- Metering M3 (tenant usage report API) and M4 (`usage_health` check).
- Transcript/recording retention CLI.
- Out-of-scope service rule in the prompt.
- Website: working early-access lead capture with consent, Privacy/Terms
  draft pages, consistent ESSR naming.
- Legal drafts, provider readiness, real-call plan, commercial package.

**Metering scope note:** the repository never defined M3/M4 in writing.
They were implemented from the launch mandate: M3 = usage reporting per
tenant, M4 = operational alerting on reconciliation and missing usage. No
billing or charging exists or was added.

## 2. Launch blockers (must be done before the first paid call)

| # | Blocker | Owner | Type |
|---|---|---|---|
| L1 | Production host + domain + TLS deployed (`docs/DEPLOYMENT.md`) | Founder | External (AWS access, DNS) |
| L2 | Apply migrations to production (and dev) DB to head `3d4e5f6a7b8c` | Founder approves | Authorization |
| L3 | Real PSTN acceptance run (`REAL_CALL_ACCEPTANCE_PLAN.md`), esp. #8–#12 | Founder runs | Real calls, costs |
| L4 | Real Twilio SMS page + human-transfer drill | Founder | Real provider actions |
| L5 | Vapi paid plan (concurrency 4 shared across all tenants on free tier) | Founder | Paid provider action |
| L6 | Legal: entity, privacy policy, terms, DPA, pilot agreement reviewed | Lawyer | Legal |
| L7 | Recording-before-notice behaviour verified on a real call; decide mitigation | Founder | Real call |
| L8 | Transfer-not-answered fallback configured in Vapi (B5) | Founder | Vapi config |
| L9 | External monitoring live (uptime + backup heartbeat + usage_health cron) | Founder | Host config |
| L10 | Push the 8+ local commits to GitHub (repo is public — review first) | Founder | Authorization |

## 3. Remaining engineering (not blocking a controlled pilot)

- Triage risk question for heat/cool loss with vulnerable occupants (audit B4)
  and deterministic hazard floor on classification (audit B5): prompt/code
  changes with tests — high value, ~2 days.
- Voice style rules (shorter turns, one question per turn) — audit B2.
- Callback follow-up when no availability is found — audit B8.
- Dashboard page for the usage report (API exists).
- Self-serve data export/erasure for businesses.
- Reschedule/cancel by phone; calendar/FSM integrations.

## 4. Day-by-day plan to 11 December

| Dates | Focus | Done when |
|---|---|---|
| Nov 9–10 | Review & push commits (L10); approve dev migration (L2-dev); send legal pack to a lawyer (L6) | `origin/main` = local; dev DB at head; lawyer engaged |
| Nov 11–13 | Provision production (L1), Vapi paid plan (L5), domain + TLS, monitoring (L9) | `/api/v1/health/ready` 200 over HTTPS; monitors green |
| Nov 14–15 | Prod migration (L2); point Vapi at prod; demo org configured | Demo line answers on prod |
| Nov 16–18 | Real-call acceptance #1–#18 (L3, L4, L7); fix findings | Signed acceptance sheet |
| Nov 19–21 | Engineering: audit B4/B5 triage + hazard floor, B2 voice style | Tests green; re-run calls #2, #8, #9 |
| Nov 22–24 | Lawyer feedback → finalize privacy/terms/DPA/pilot agreement; remove "draft" banners | Documents signed off |
| Nov 25–28 | Prospect list (50 local HVAC/plumbing/electrical), qualification; outreach starts after legal gate | 50 qualified prospects; first 20 emails sent |
| Nov 29–Dec 5 | Demos (DEMO_SCRIPT), follow-ups, first pilot agreement | ≥3 demos; ≥1 verbal yes |
| Dec 6–10 | First customer onboarding (ONBOARDING_AND_IMPLEMENTATION), owner sign-off | Customer line live, 2 days of supervised calls |
| Dec 11 | Commercial target: first paid pilot live | Invoice sent for month 1 |

## 5. Realistic assessment

The software is ready for a **controlled, supervised paid pilot** once L1–L9
are done. It is not production-proven: it has never run on a production
host, never sent a real SMS page, never completed a real transfer, and has
handled only a handful of real calls. Treat the first customer as a pilot
with daily review, founder on call, and an explicit "not an emergency
service" agreement.
