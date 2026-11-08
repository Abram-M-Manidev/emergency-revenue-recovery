# Security incident response — DRAFT

For a one-operator company. Practise it once before the first paid pilot.

1. **Detect.** Sources: uptime monitor, backup heartbeat, `usage_health`
   alerts, provider security notices, customer reports, unusual log events
   (`voice_line_assistant_mismatch`, auth failures, rate-limit spikes).
2. **Contain (first hour).**
   - Leaked secret: rotate it (RUNBOOK "Rotating secrets"). Vapi secret
     rotation needs three updates (see RUNBOOK).
   - Compromised tenant account: disable the user; revoke refresh tokens.
   - Misbehaving assistant on a tenant: use the per-tenant kill switch
     (RUNBOOK "Disabling one tenant's voice assistant").
   - Host compromise: snapshot disk for evidence, rebuild from code + last
     good backup, rotate every secret.
3. **Assess.** Which tenants, which data categories, which time window.
   Logs never contain caller names/addresses by design; the database does.
4. **Notify.** Affected businesses without undue delay and within [72]
   hours (per DPA). COUNSEL: regulator notification duties (India CERT-In
   reports within 6 hours for specified incidents; DPDP Board; US state
   breach laws depend on data and residence).
5. **Recover and review.** Fix root cause, add a regression test, write a
   short post-incident note with dates and decisions.

Contacts: [OPERATOR PHONE], [OPERATOR EMAIL], [LAWYER], provider security
contacts (Vapi, OpenAI, Twilio, hosting).
